# JOHNSON MESH ULTRA
# LIGHTWEIGHT + LOCK MODE
# Johnson Algorithm PRESERVED (Research Core)

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, ether_types, arp
from ryu.topology import event, api as topology_api
from ryu.lib import hub
import networkx as nx
import gc

class JohnsonMeshUltraController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.topology_api_app = self

        self.hosts = {}
        self.arp_table = {}

        self.net = nx.DiGraph()
        self.port_map = {}
        self.mst = None

        # JOHNSON RESULT (NEXT-HOP ONLY)
        self.johnson_nexthop = {}

        self.topology_frozen = False
        self.is_ready = False
        self.last_stats = (-1, -1)

        self.logger.info(">>> JohnsonMeshUltraController READY (JOHNSON LOCK MODE)")
        hub.spawn(self._monitor_topology)

    # ---------------- FLOW INIT ----------------
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        dp = ev.msg.datapath
        ofp = dp.ofproto
        parser = dp.ofproto_parser

        self.add_flow(dp, 0, parser.OFPMatch(), [])

        actions = [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)]
        self.add_flow(dp, 65535,
                      parser.OFPMatch(eth_type=ether_types.ETH_TYPE_LLDP),
                      actions)
        self.add_flow(dp, 100,
                      parser.OFPMatch(eth_type=ether_types.ETH_TYPE_ARP),
                      actions)

    def add_flow(self, dp, priority, match, actions):
        parser = dp.ofproto_parser
        ofp = dp.ofproto
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        dp.send_msg(parser.OFPFlowMod(
            datapath=dp, priority=priority,
            match=match, instructions=inst
        ))

    # ---------------- IGNORE TOPO EVENTS ----------------
    @set_ev_cls([event.EventLinkAdd, event.EventLinkDelete, event.EventSwitchEnter])
    def _ignore_events(self, ev):
        pass

    # ---------------- TOPO MONITOR ----------------
    def _monitor_topology(self):
        hub.sleep(80)

        while True:
            if self.topology_frozen:
                self.logger.info(">>> TOPOLOGY LOCKED | MONITOR STOPPED")
                break

            switches = topology_api.get_switch(self.topology_api_app, None)
            links = topology_api.get_link(self.topology_api_app, None)

            sw, lk = len(switches), len(links)
            if (sw, lk) != self.last_stats:
                self.logger.info(f">>> STATUS: {sw} Switch | {lk} Link")
                self.last_stats = (sw, lk)

            TARGET_LINKS = sw * (sw - 1)
            if lk >= TARGET_LINKS and sw > 1:
                self.logger.info(">>> FULL MESH DETECTED | RUN JOHNSON")
                self._run_johnson(switches, links)
                self.topology_frozen = True
                self.is_ready = True
                gc.collect()

            hub.sleep(10)

    # ---------------- JOHNSON CORE ----------------
    def _run_johnson(self, switches, links):
        g = nx.DiGraph()
        port_map = {}

        for s in switches:
            g.add_node(s.dp.id)
            port_map[s.dp.id] = {}

        for l in links:
            g.add_edge(l.src.dpid, l.dst.dpid, port=l.src.port_no, weight=1)
            g.add_edge(l.dst.dpid, l.src.dpid, port=l.dst.port_no, weight=1)
            port_map[l.src.dpid][l.src.port_no] = l.dst.dpid
            port_map[l.dst.dpid][l.dst.port_no] = l.src.dpid

        self.net = g
        self.port_map = port_map

        # MST (FLOOD SAFETY)
        undirected = g.to_undirected()
        self.mst = nx.minimum_spanning_tree(undirected)

        # JOHNSON ALGORITHM
        johnson_paths = nx.johnson(g, weight='weight')

        # CONVERT TO NEXT-HOP (MEMORY SAFE)
        self.johnson_nexthop = {}
        for src, dsts in johnson_paths.items():
            self.johnson_nexthop[src] = {}
            for dst, path in dsts.items():
                if len(path) > 1:
                    self.johnson_nexthop[src][dst] = path[1]

        self.logger.info(">>> JOHNSON COMPUTATION DONE | SYSTEM LOCKED")

    # ---------------- ARP PROXY ----------------
    def _handle_arp(self, dp, in_port, pkt):
        self.arp_table[pkt.src_ip] = pkt.src_mac
        self.hosts[pkt.src_mac] = (dp.id, in_port)

        if pkt.opcode == arp.ARP_REQUEST and pkt.dst_ip in self.arp_table:
            self._send_arp_reply(dp, in_port,
                                 pkt.src_mac, pkt.src_ip,
                                 self.arp_table[pkt.dst_ip], pkt.dst_ip)
            return True
        return False

    def _send_arp_reply(self, dp, port, dst_mac, dst_ip, src_mac, src_ip):
        pkt = packet.Packet()
        pkt.add_protocol(ethernet.ethernet(
            ethertype=ether_types.ETH_TYPE_ARP,
            dst=dst_mac, src=src_mac))
        pkt.add_protocol(arp.arp(
            opcode=arp.ARP_REPLY,
            src_mac=src_mac, src_ip=src_ip,
            dst_mac=dst_mac, dst_ip=dst_ip))
        pkt.serialize()

        dp.send_msg(dp.ofproto_parser.OFPPacketOut(
            datapath=dp,
            buffer_id=dp.ofproto.OFP_NO_BUFFER,
            in_port=dp.ofproto.OFPP_CONTROLLER,
            actions=[dp.ofproto_parser.OFPActionOutput(port)],
            data=pkt.data))

    # ---------------- MST FLOOD ----------------
    def _intelligent_flood(self, dp, in_port, msg):
        if not self.mst:
            return

        parser = dp.ofproto_parser
        actions = []

        for p in dp.ports.values():
            if p.port_no == in_port or p.port_no > dp.ofproto.OFPP_MAX:
                continue
            neigh = self.port_map.get(dp.id, {}).get(p.port_no)
            if neigh is None or self.mst.has_edge(dp.id, neigh):
                actions.append(parser.OFPActionOutput(p.port_no))

        if actions:
            dp.send_msg(parser.OFPPacketOut(
                datapath=dp,
                buffer_id=msg.buffer_id,
                in_port=in_port,
                actions=actions,
                data=msg.data))

    # ---------------- PACKET IN ----------------
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):
        if not self.is_ready:
            return

        msg = ev.msg
        dp = msg.datapath
        dpid = dp.id
        in_port = msg.match['in_port']
        parser = dp.ofproto_parser

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocols(ethernet.ethernet)[0]

        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        if eth.ethertype == ether_types.ETH_TYPE_ARP:
            pkt_arp = pkt.get_protocols(arp.arp)[0]
            if self._handle_arp(dp, in_port, pkt_arp):
                return

        src, dst = eth.src, eth.dst
        self.hosts.setdefault(src, (dpid, in_port))

        if dst not in self.hosts or dst == 'ff:ff:ff:ff:ff:ff':
            self._intelligent_flood(dp, in_port, msg)
            return

        dst_dpid, dst_port = self.hosts[dst]

        if dpid == dst_dpid:
            actions = [parser.OFPActionOutput(dst_port)]
        elif dpid in self.johnson_nexthop and dst_dpid in self.johnson_nexthop[dpid]:
            nh = self.johnson_nexthop[dpid][dst_dpid]
            out_port = self.net[dpid][nh]['port']
            actions = [parser.OFPActionOutput(out_port)]
        else:
            self._intelligent_flood(dp, in_port, msg)
            return

        match = parser.OFPMatch(eth_src=src, eth_dst=dst)
        self.add_flow(dp, 1, match, actions)

        dp.send_msg(parser.OFPPacketOut(
            datapath=dp,
            buffer_id=msg.buffer_id,
            in_port=in_port,
            actions=actions,
            data=msg.data))
