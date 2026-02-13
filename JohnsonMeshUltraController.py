# Copyright (C) 2011 Nippon Telegraph and Telephone Corporation.
# Licensed under the Apache License, Version 2.0
#
# CONTROLLER MESH - GATEKEEPER EDITION (50 NODE SUCCESS)
# Fitur:
# 1. Initial Lock 650 detik
# 2. Monitor Interval 10 detik
# 3. Inactivity Probe 60 detik

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

class JohnsonMeshUltraController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(JohnsonMeshUltraController, self).__init__(*args, **kwargs)
        self.topology_api_app = self
        self.hosts = {}
        self.net = nx.DiGraph()
        self.mst = None
        self.all_paths = {}
        self.port_map = {}
       
        self.start_time = time.time()
        # INITIAL LOCK 650 DETIK (Disesuaikan untuk sukses 50 node)
        # 10 node pakai 60
        self.initial_lock = 650
        self.is_ready = False
        self.calc_in_progress = False
       
        self.logger.info("JohnsonMeshUltraController (Gatekeeper): Ready.")
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
            hub.sleep(10.0) # Monitor interval 10 detik
           
            switches = topology_api.get_switch(self.topology_api_app, None)
            links = topology_api.get_link(self.topology_api_app, None)
            sw_count = len(switches)
            lnk_count = len(links)
           
            elapsed = time.time() - self.start_time

            # LOGIKA GATEKEEPER
            # 1. Jika masih dalam masa tunggu (Boot Phase), PASTIKAN is_ready = FALSE
            if elapsed < self.initial_lock:
                self.is_ready = False
                self.logger.info(f">>> BOOT PHASE: Waiting {int(self.initial_lock - elapsed)}s more. Links: {lnk_count}")
                
                # Hitung di background untuk persiapan, tapi traffic tetap drop
                if sw_count > 0 and not self.calc_in_progress:
                    self.calc_in_progress = True
                    safe_links = [(l.src.dpid, l.dst.dpid, l.src.port_no, l.dst.port_no) for l in links]
                    safe_switches = [s.dp.id for s in switches]
                    tpool.execute(self._calculate_logic, safe_switches, safe_links)
            
            # 2. Jika waktu habis (Post-Boot), Trigger perhitungan ULANG akhir
            # JANGAN set is_ready di sini. Biarkan fungsi _calculate_logic yang menentukan.
            else:
                self.logger.info(f">>> TIME ELAPSED. Finalizing Topology Calculation...")
                if not self.calc_in_progress:
                    self.calc_in_progress = True
                    safe_links = [(l.src.dpid, l.dst.dpid, l.src.port_no, l.dst.port_no) for l in links]
                    safe_switches = [s.dp.id for s in switches]
                    tpool.execute(self._calculate_logic, safe_switches, safe_links)

            gc.collect()

    def _calculate_logic(self, switch_ids, link_list):
        """
        Dijalankan di Background Thread.
        Tempat PEMBUKAAN GERBANG TRAFFIC.
        """
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
           
            # Hitung MST
            undirected = temp_net.to_undirected()
            if nx.is_connected(undirected):
                mst = nx.minimum_spanning_tree(undirected)
            else:
                mst = None

            # Hitung Johnson
            all_paths = nx.johnson(temp_net, weight='weight')

            # Update Global State (Atomic)
            self.net = temp_net
            self.port_map = temp_port_map
            self.mst = mst
            self.all_paths = all_paths
            
            self.calc_in_progress = False
            
            # LOGIKA GATEKEEPER KHUSUS
            # Hanya buka gerbang traffic jika:
            # 1. Perhitungan SUKSES (kita ada di sini)
            # 2. WAKTU SUDAH LEBIH DARI LOCK TIME
            elapsed = time.time() - self.start_time
            if elapsed >= self.initial_lock:
                self.is_ready = True # <--- PERUBAHAN KRUSIAL: BUKA DISINI
                self.logger.info(f">>> CALCULATION DONE & TIME EXPIRED. >>> TRAFFIC ALLOWED.")
            else:
                self.logger.info(f">>> CALCULATION DONE. Still waiting for timer ({int(elapsed)}s).")
           
        except Exception as e:
            self.logger.error(f"Calculation Error: {e}")
            self.calc_in_progress = False
            # Jangan set is_ready jika error

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
                # Port ke host
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

        # Parsing dulu
        try:
            pkt = packet.Packet(msg.data)
            eth = pkt.get_protocols(ethernet.ethernet)[0]
        except:
            return

        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return

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
            if (self.all_paths and
                dpid in self.all_paths and
                dst_dpid in self.all_paths[dpid]):
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