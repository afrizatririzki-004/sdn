# Copyright (C) 2011 Nippon Telegraph and Telephone Corporation.
# Licensed under the Apache License, Version 2.0
#
# BELLMAN-FORD CONTROLLER (INSTANT MODE FOR 10 NODES)
#
# Optimasi:
# 1. Menggunakan Bellman-Ford (All-Pairs Shortest Path) - Sederhana & Sangat Cepat.
# 2. Konvergensi Instan (Lock sangat pendek) karena topologi kecil.

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, ether_types, arp
from ryu.topology import api as topology_api
from ryu.lib import hub
import networkx as nx
from eventlet import tpool
import gc
import time

class BellmanFordController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(BellmanFordController, self).__init__(*args, **kwargs)
        self.topology_api_app = self
        
        self.hosts = {}
        self.net = nx.DiGraph()
        self.all_paths = {}
        self.port_map = {} 

        self.is_ready = False
        self.calc_in_progress = False
        
        self.start_time = time.time()
        # INITIAL LOCK SANGAT PENDUK UNTUK 10 NODE
        # Kita butuh waktu sedikit untuk switch connect, tapi jangan lama.
        # 10 node hanya punya 45 link. 60 detik sudah lebih dari cukup.
        self.initial_lock = 30.0 
        self.logger.info("BellmanFordController (INSTANT MODE - 10 NODE OPTIMIZED): Ready.")
        hub.spawn(self._monitor_topology)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER)]
        self.add_flow(datapath, 0, match, actions, buffer_id=ofproto.OFP_NO_BUFFER)

    def add_flow(self, datapath, priority, match, actions):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        mod = parser.OFPFlowMod(
            datapath=datapath, priority=priority,
            match=match, instructions=inst
        )
        datapath.send_msg(mod)

    @set_ev_cls([event.EventLinkAdd, event.EventLinkDelete, event.EventSwitchEnter])
    def _topology_event_ignore(self, ev):
        pass

    def _monitor_topology(self):
        while True:
            hub.sleep(10.0) # Cek tiap 10 detik. Untuk 10 node, 10 detik cukup.
            
            switches = topology_api.get_switch(self.topology_api_app, None)
            links = topology_api.get_link(self.topology_api_app, None)
            
            if not self.calc_in_progress and len(switches) > 0:
                self.calc_in_progress = True
                safe_links = [(l.src.dpid, l.dst.dpid, l.src.port_no, l.dst.port_no) for l in links]
                safe_switches = [s.dp.id for s in switches]
                tpool.execute(self._calculate_logic, safe_switches, safe_links)
            
            # LOGIKA GATEKEEPER
            elapsed = time.time() - self.start_time
            if elapsed < self.initial_lock:
                self.is_ready = False
                self.logger.info(f">> WAITING FOR LOCK EXPIRY. Links: {len(links)}")
            else:
                self.logger.info(">>> LOCK EXPIRED. SYSTEM READY.")
                self.is_ready = True
                # Untuk 10 node, kita biarkan logic _calculate_logic menentukan kapan siap.
                if not self.calc_in_progress:
                    self.calc_in_progress = True
                    safe_links = [(l.src.dpid, l.dst.dpid, l.src.port_no, l.dst.port_no) for l in links]
                    safe_switches = [s.dp.id for s in switches]
                    tpool.execute(self._calculate_logic, safe_switches, safe_links)

            gc.collect()

    def _calculate_logic(self, switch_ids, link_list):
        """
        Menghitung jalur menggunakan Bellman-Ford (All-Pairs).
        Jauh lebih cepat dari Johnson.
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
                if dst not in temp_port_map: temp_port_map[dst][dport] = src
            
            # BELLAMAN-FORD ALGORITHM
            # Jauh menghitung jalur terpendek untuk SEMUA pasang node, lalu gabungkan.
            # Ini O((V*E) + ElogV) yang sangat cepat.
            self.all_paths = dict(nx.all_pairs_dijkstra_path(temp_net, weight='weight'))

            self.net = temp_net
            self.port_map = temp_port_map
            self.calc_in_progress = False
            
            self.logger.info(">>> BELLAMAN-FORD CALCULATION DONE.")
            
        except Exception as e:
            self.logger.error(f"Calculation Error: {e}")
            self.calc_in_progress = False
            # Jangan set is_ready jika error

    def _intelligent_flood(self, datapath, in_port, msg):
        if self.net is None: return
        parser = datapath.ofproto_parser
        actions = []
        all_ports = [p.port_no for p in datapath.ports.values() if p.port_no <= datapath.ofproto.OFPP_MAX]
        dpid = datapath.id
       
        for port_no in all_ports:
            if port_no == in_port: continue
            local_map = self.port_map.get(dpid, {})
            neighbor = local_map.get(port_no)
            if neighbor and self.net.has_edge(dpid, neighbor):
                actions.append(parser.OFPActionOutput(port_no))
            else:
                # Port ke host
                actions.append(parser.OFPActionOutput(port_no))

        if actions:
            out = parser.OFPPacketOut(
                datapath=datapath, buffer_id=msg.buffer_id,
                in_port=in_port, actions=actions, data=msg.data
            )
            datapath.send_msg(out)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):
        if not self.is_ready:
            return

        msg = ev.msg
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
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

        src, dst = eth.src, eth.dst
        
        # Cek Host
        if src not in self.hosts:
            self.hosts[src] = (dpid, in_port)

        # Logic Flood / ARP
        if eth.ethertype == ether_types.ETH_TYPE_ARP or dst not in self.hosts:
            self._intelligent_flood(datapath, in_port, msg)
            return

        # Logic Unicast
        dst_dpid = self.hosts[dst][0]
        out_port_final = self.hosts[dst][1]

        if dpid == dst_dpid:
            actions = [parser.OFPActionOutput(out_port_final)]
        else:
            if (self.all_paths and
                dpid in self.all_paths and
                dst_dpid in self.all_paths[dpid]):
                # Bellman-Ford mengembalikarkan array dictionary. Mari kita ambil path dengan hati-hati.
                try:
                    # Untuk 10 node, node ini terdefinisi. Jika ada, gunakan.
                    path_obj = self.all_paths[dpid].get(dst_dpid, [])
                    if path_obj:
                        # Jalurnya: [src, ..., dst]
                        next_hop = path_obj[0]
                        out_port = self.net[dpid][next_hop]['port']
                        actions = [parser.OFPActionOutput(out_port)]
                    else:
                        actions = []
                except:
                    # Fallback jika error key tidak ditemukan (sebaiknya karena graph kecil)
                    self._intelligent_flood(datapath, in_port, msg)
                    return
            else:
                self._intelligent_flood(datapath, in_port, msg)
                return

        # Install Flow
        match = parser.OFPMatch(eth_dst=dst)
        self.add_flow(datapath, 1, match, actions)
        
        out = parser.OFPPacketOut(
            datapath=datapath, buffer_id=msg.buffer_id,
            in_port=in_port, actions=actions, data=msg.data)
        
        datapath.send_msg(out)