# Copyright (C) 2011 Nippon Telegraph and Telephone Corporation.
# Licensed under the Apache License, Version 2.0
#
# CONTROLLER MESH - GATEKEEPER EDITION (10 NODE ADJUSTED) - BELLMAN FORD
# Fitur:
# 1. Initial Lock 20 detik (Disesuaikan agar sinkron dengan test script 10 node)
# 2. Monitor Interval 10 detik
# 3. Algorithm: Bellman-Ford

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
import time

class BellmanFordMeshController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(BellmanFordMeshController, self).__init__(*args, **kwargs)
        self.topology_api_app = self
        self.hosts = {}
        self.net = nx.DiGraph()
        self.mst = None
        self.all_paths = {}
        self.port_map = {}
        
        self.start_time = time.time()
        
        # --- PERUBAHAN PENTING DI SINI ---
        # Gunakan 20.0 untuk tes 10 node. 
        # Gunakan 1250.0 HANYA jika tes 50 node.
        self.initial_lock = 700.0
        
        self.is_ready = False
        self.calc_in_progress = False
        
        self.logger.info("BellmanFordMeshController (Node Config): Ready.")
        hub.spawn(self._monitor_topology)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER)]
        self.add_flow(datapath, 0, match, actions, buffer_id=ofproto.OFP_NO_BUFFER)

    def add_flow(self, datapath, priority, match, actions, buffer_id=None):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        if buffer_id is None:
            buffer_id = ofproto.OFP_NO_BUFFER
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        mod = parser.OFPFlowMod(datapath=datapath, priority=priority,
                                match=match, instructions=inst, buffer_id=buffer_id)
        datapath.send_msg(mod)

    @set_ev_cls([event.EventLinkAdd, event.EventLinkDelete, event.EventSwitchEnter])
    def _topology_event_ignore(self, ev):
        pass

    def _monitor_topology(self):
        while True:
            hub.sleep(10.0) # Dipercepat sedikit intervalnya untuk 10 node
            
            switches = topology_api.get_switch(self.topology_api_app, None)
            links = topology_api.get_link(self.topology_api_app, None)
            sw_count = len(switches)
            lnk_count = len(links)
            
            elapsed = time.time() - self.start_time

            # LOGIKA GATEKEEPER
            if elapsed < self.initial_lock:
                self.is_ready = False
                self.logger.info(f">>> BOOT PHASE (BF): Waiting {int(self.initial_lock - elapsed)}s more. Links: {lnk_count}")
                
                # Pre-calculate
                if sw_count > 0 and not self.calc_in_progress:
                    self.calc_in_progress = True
                    safe_links = [(l.src.dpid, l.dst.dpid, l.src.port_no, l.dst.port_no) for l in links]
                    safe_switches = [s.dp.id for s in switches]
                    tpool.execute(self._calculate_logic, safe_switches, safe_links)
            
            else:
                self.logger.info(f">>> TIME ELAPSED. Finalizing Topology Calculation (Bellman-Ford)...")
                if not self.calc_in_progress:
                    self.calc_in_progress = True
                    safe_links = [(l.src.dpid, l.dst.dpid, l.src.port_no, l.dst.port_no) for l in links]
                    safe_switches = [s.dp.id for s in switches]
                    tpool.execute(self._calculate_logic, safe_switches, safe_links)

            gc.collect()

    def _calculate_logic(self, switch_ids, link_list):
        try:
            temp_net = nx.DiGraph()
            temp_port_map = {}

            for dpid in switch_ids:
                temp_net.add_node(dpid)
                if dpid not in temp_port_map: temp_port_map[dpid] = {}

            for src, dst, sport, dport in link_list:
                temp_net.add_edge(src, dst, port=sport, weight=1)
                temp_net.add_edge(dst, src, port=dport, weight=1)
                
                if src in temp_port_map: temp_port_map[src][sport] = dst
                if dst in temp_port_map: temp_port_map[dst][dport] = src
            
            # MST untuk Intelligent Flood
            undirected = temp_net.to_undirected()
            if nx.is_connected(undirected):
                mst = nx.minimum_spanning_tree(undirected)
            else:
                mst = None

            # BELLMAN-FORD IMPLEMENTATION
            all_paths = {}
            for node in temp_net.nodes():
                try:
                    paths = nx.single_source_bellman_ford_path(temp_net, node, weight='weight')
                    all_paths[node] = paths
                except nx.NetworkXUnbounded:
                    pass
            
            # Atomic Update
            self.net = temp_net
            self.port_map = temp_port_map
            self.mst = mst
            self.all_paths = all_paths
            
            self.calc_in_progress = False
            
            # UNLOCK TRAFFIC
            elapsed = time.time() - self.start_time
            if elapsed >= self.initial_lock:
                self.is_ready = True 
                self.logger.info(f">>> CALCULATION (BF) DONE & TIME EXPIRED. >>> TRAFFIC ALLOWED.")
            else:
                self.logger.info(f">>> CALCULATION (BF) DONE. Still waiting for timer ({int(self.initial_lock - elapsed)}s).")
            
        except Exception as e:
            self.logger.error(f"Calculation Error: {e}")
            self.calc_in_progress = False

    def _intelligent_flood(self, datapath, in_port, msg):
        if self.mst is None: return
        parser = datapath.ofproto_parser
        actions = []
        all_ports = [p.port_no for p in datapath.ports.values() if p.port_no <= datapath.ofproto.OFPP_MAX]
        dpid = datapath.id
        
        for port_no in all_ports:
            if port_no == in_port: continue
            local_map = self.port_map.get(dpid, {})
            neighbor = local_map.get(port_no)
            if neighbor:
                if self.mst.has_edge(dpid, neighbor):
                    actions.append(parser.OFPActionOutput(port_no))
            else:
                actions.append(parser.OFPActionOutput(port_no))

        if actions:
            out = parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id,
                                      in_port=in_port, actions=actions, data=msg.data)
            datapath.send_msg(out)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        parser = datapath.ofproto_parser
        in_port = msg.match['in_port']
        dpid = datapath.id

        try:
            pkt = packet.Packet(msg.data)
            eth = pkt.get_protocols(ethernet.ethernet)[0]
        except:
            return

        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        # Traffic Block
        if not self.is_ready:
            return

        dst = eth.dst
        src = eth.src
        
        if src not in self.hosts:
            self.hosts[src] = (dpid, in_port)

        if eth.ethertype == ether_types.ETH_TYPE_ARP or dst not in self.hosts:
            self._intelligent_flood(datapath, in_port, msg)
            return

        dst_dpid = self.hosts[dst][0]
        
        if dpid == dst_dpid:
            actions = [parser.OFPActionOutput(self.hosts[dst][1])]
        else:
            if (self.all_paths and dpid in self.all_paths and dst_dpid in self.all_paths[dpid]):
                try:
                    path = self.all_paths[dpid][dst_dpid]
                    next_hop = path[path.index(dpid) + 1]
                    out_port = self.net[dpid][next_hop]['port']
                    actions = [parser.OFPActionOutput(out_port)]
                except:
                    self._intelligent_flood(datapath, in_port, msg)
                    return
            else:
                self._intelligent_flood(datapath, in_port, msg)
                return

        match = parser.OFPMatch(eth_dst=dst)
        self.add_flow(datapath, 1, match, actions)
        
        out = parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id,
                                  in_port=in_port, actions=actions, data=msg.data)
        datapath.send_msg(out)