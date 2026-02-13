# Copyright (C) 2011 Nippon Telegraph and Telephone Corporation.
# Licensed under the Apache License, Version 2.0
# 
# CONTROLLER MESH - JOHNSON (ARP PROXY + STABILITY FIX)
# 
# Perbaikan Utama:
# 1. STABILITY CHECK: Hanya menghitung rute jika topologi STABIL selama 10 detik.
#    (Mencegah perhitungan berulang saat 'flapping' yang mematikan CPU).
# 2. ARP PROXY: Tetap aktif untuk mencegah Broadcast Storm.
# 3. TPOOL: Menghitung di background thread.

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, ether_types, arp
from ryu.topology import event, api as topology_api
from ryu.lib import hub
import networkx as nx
from eventlet import tpool
import gc 

class JohnsonMeshProxyController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(JohnsonMeshProxyController, self).__init__(*args, **kwargs)
        self.topology_api_app = self
        self.hosts = {}      # Mapping MAC -> (dpid, port)
        self.arp_table = {}  # Mapping IP -> MAC (Untuk Proxy)
        self.net = nx.DiGraph()
        self.mst = None
        self.all_paths = {} 
        self.port_map = {} 
        
        # Variabel Stabilitas
        self.last_topo_stats = (-1, -1) # (Switch Count, Link Count)
        self.stability_counter = 0      # Counter kestabilan
        self.last_log_info = (-1, -1, "") 
        
        self.logger.info("JohnsonMeshProxyController: Siap (Stability Check Mode).")
        hub.spawn(self._monitor_topology)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                          ofproto.OFPCML_NO_BUFFER)]
        self.add_flow(datapath, 0, match, actions)

    def add_flow(self, datapath, priority, match, actions, buffer_id=None):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        mod_class = parser.OFPFlowMod
        if buffer_id:
            mod = mod_class(datapath=datapath, buffer_id=buffer_id,
                            priority=priority, match=match, instructions=inst)
        else:
            mod = mod_class(datapath=datapath, priority=priority,
                            match=match, instructions=inst)
        datapath.send_msg(mod)

    @set_ev_cls([event.EventLinkAdd, event.EventLinkDelete, event.EventSwitchEnter])
    def _topology_event_ignore(self, ev):
        pass

    def _monitor_topology(self):
        """
        Loop Monitor Cerdas:
        Hanya memicu perhitungan berat jika topologi benar-benar TENANG.
        """
        while True:
            hub.sleep(5.0) # Cek setiap 5 detik
            
            # Cek jumlah switch/link saat ini
            switches = topology_api.get_switch(self.topology_api_app, None)
            links = topology_api.get_link(self.topology_api_app, None)
            
            current_stats = (len(switches), len(links))
            
            # Logika Debounce/Stabilitas
            if current_stats != self.last_topo_stats:
                # Jika berubah, RESET counter. Jangan hitung dulu!
                self.logger.info(f">>> Flapping Detect: {current_stats[0]} Sw, {current_stats[1]} Link. Menunggu Stabil...")
                self.last_topo_stats = current_stats
                self.stability_counter = 0
            else:
                # Jika sama dengan cek sebelumnya, tambah counter
                self.stability_counter += 1

            # HANYA hitung jika sudah stabil selama 10 detik (counter == 2)
            # Counter > 2 artinya sudah dihitung, tidak perlu hitung lagi.
            if self.stability_counter == 2 and current_stats[0] > 0:
                self.logger.info(f">>> TOPOLOGI STABIL ({current_stats}). Menghitung Johnson di Background...")
                self._build_optimal_topology(switches, links)
                gc.collect()

    def _build_optimal_topology(self, switches, links):
        temp_net = nx.DiGraph()
        temp_port_map = {} 

        for switch in switches:
            dpid = switch.dp.id
            temp_net.add_node(dpid)
            if dpid not in temp_port_map: temp_port_map[dpid] = {}

        for link in links:
            src, dst = link.src.dpid, link.dst.dpid
            src_port, dst_port = link.src.port_no, link.dst.port_no
            
            # Johnson Weight = 1
            temp_net.add_edge(src, dst, port=src_port, weight=1)
            temp_net.add_edge(dst, src, port=dst_port, weight=1)
            
            if src in temp_port_map: temp_port_map[src][src_port] = dst
            if dst in temp_port_map: temp_port_map[dst][dst_port] = src
        
        self.net = temp_net
        self.port_map = temp_port_map 

        if len(self.net.nodes) > 0:
            try:
                undirected = self.net.to_undirected()
                if nx.is_connected(undirected):
                    # Hitung MST di thread terpisah
                    self.mst = tpool.execute(nx.minimum_spanning_tree, undirected)
                else:
                    self.mst = None
            except:
                self.mst = None

        if len(self.net.nodes) > 0:
            try:
                # Hitung Johnson di thread terpisah
                self.all_paths = tpool.execute(nx.johnson, self.net, weight='weight')
            except Exception:
                self.all_paths = {}
        else:
            self.all_paths = {}

        if len(self.net.nodes) > 0:
            ready_msg = "PARTIAL"
            if self.mst and self.all_paths: ready_msg = "FULL/JOHNSON READY"
            
            self.logger.info(f">>> Mesh Update Selesai: {len(self.net.nodes)} Sw, {len(self.net.edges)} Link (Status: {ready_msg})")

    # --- LOGIKA ARP PROXY ---
    def _handle_arp(self, datapath, in_port, pkt_arp, eth):
        src_ip = pkt_arp.src_ip
        src_mac = pkt_arp.src_mac
        dst_ip = pkt_arp.dst_ip

        # Pelajari IP -> MAC
        self.arp_table[src_ip] = src_mac
        self.hosts[src_mac] = (datapath.id, in_port)

        if pkt_arp.opcode == arp.ARP_REQUEST:
            if dst_ip in self.arp_table:
                # Proxy Reply
                dst_mac = self.arp_table[dst_ip]
                self._send_arp_reply(datapath, in_port, src_mac, src_ip, dst_mac, dst_ip)
                return True
        return False

    def _send_arp_reply(self, datapath, port, src_mac, src_ip, target_mac, target_ip):
        pkt = packet.Packet()
        pkt.add_protocol(ethernet.ethernet(ethertype=ether_types.ETH_TYPE_ARP,
                                           dst=src_mac, src=target_mac))
        pkt.add_protocol(arp.arp(opcode=arp.ARP_REPLY,
                                 src_mac=target_mac, src_ip=target_ip,
                                 dst_mac=src_mac, dst_ip=src_ip))
        pkt.serialize()
        actions = [datapath.ofproto_parser.OFPActionOutput(port)]
        out = datapath.ofproto_parser.OFPPacketOut(
            datapath=datapath, buffer_id=datapath.ofproto.OFP_NO_BUFFER,
            in_port=datapath.ofproto.OFPP_CONTROLLER, actions=actions, data=pkt.data)
        datapath.send_msg(out)

    def _intelligent_flood(self, datapath, in_port, msg):
        if self.mst is None: return

        parser = datapath.ofproto_parser
        actions = []
        all_ports = [p.port_no for p in datapath.ports.values() if p.port_no <= datapath.ofproto.OFPP_MAX]
        dpid = datapath.id
        
        for port_no in all_ports:
            if port_no == in_port: continue
            
            local_map = self.port_map.get(dpid, {})
            neighbor_dpid = local_map.get(port_no)
            
            if neighbor_dpid:
                # Link antar switch: Ikuti MST
                if self.mst.has_edge(dpid, neighbor_dpid):
                    actions.append(parser.OFPActionOutput(port_no))
            else:
                # Port Host
                actions.append(parser.OFPActionOutput(port_no))

        if actions:
            out = parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id,
                                      in_port=in_port, actions=actions, data=msg.data)
            datapath.send_msg(out)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        dpid = datapath.id
        parser = datapath.ofproto_parser
        in_port = msg.match['in_port']

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocols(ethernet.ethernet)[0]
        
        if eth.ethertype == ether_types.ETH_TYPE_LLDP: return

        # Handle ARP dengan Proxy
        if eth.ethertype == ether_types.ETH_TYPE_ARP:
            pkt_arp = pkt.get_protocols(arp.arp)[0]
            handled = self._handle_arp(datapath, in_port, pkt_arp, eth)
            if handled:
                return 

        dst = eth.dst
        src = eth.src
        
        if src not in self.hosts:
            self.hosts[src] = (dpid, in_port)

        # Flood jika broadcast non-ARP atau unknown unicast
        if dst == 'ff:ff:ff:ff:ff:ff' or dst not in self.hosts:
            self._intelligent_flood(datapath, in_port, msg)
            return

        dst_dpid = self.hosts[dst][0]
        
        if dpid == dst_dpid:
            actions = [parser.OFPActionOutput(self.hosts[dst][1])]
        else:
            # Routing Johnson
            if (self.all_paths and 
                dpid in self.all_paths and 
                dst_dpid in self.all_paths[dpid]):
                
                path = self.all_paths[dpid][dst_dpid]
                next_hop = path[path.index(dpid) + 1]
                out_port = self.net[dpid][next_hop]['port']
                actions = [parser.OFPActionOutput(out_port)]
            else:
                return 

        match = parser.OFPMatch(eth_dst=dst)
        self.add_flow(datapath, 1, match, actions)
        
        out = parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id,
                                  in_port=in_port, actions=actions, data=msg.data)
        datapath.send_msg(out)