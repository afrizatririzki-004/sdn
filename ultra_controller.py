# Copyright (C) 2011 Nippon Telegraph and Telephone Corporation.
# Licensed under the Apache License, Version 2.0
#
# REACTIVE JOHNSON CONTROLLER (FIXED FOR 100 NODES)
#
# Pendekatan: Lazy Initialization (Tunggu Paket pertama baru hitung)
# Menghindari: File I/O, Deadlock, dan Thread termination.

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, ether_types, arp
from ryu.topology import api as topology_api
import networkx as nx
import time

class JohnsonMeshUltraController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(JohnsonMeshUltraController, self).__init__(*args, **kwargs)
        
        self.hosts = {}
        self.net = nx.DiGraph()
        self.mst = None
        self.all_paths = {}
        self.port_map = {}

        self.is_ready = False
        self.calc_in_progress = False
        
        self.logger.info(">>> JohnsonMeshUltraController (REACTIVE MODE) READY.")
        self.logger.info(">>> Will calculate Johnson on first packet arrival...")

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        # Table-miss flow
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER)]
        self.add_flow(datapath, 0, match, actions)

    def add_flow(self, datapath, priority, match, actions):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        mod = parser.OFPFlowMod(
            datapath=datapath, priority=priority,
            match=match, instructions=inst
        )
        datapath.send_msg(mod)

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
                actions.append(parser.OFPActionOutput(port_no)) # Host

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

        # --- REACTIVE TRIGGER (ONE TIME ONLY) ---
        if not self.is_ready:
            self.logger.info(">>> FIRST PACKET RECEIVED. STARTING TOPOLOGY DISCOVERY...")
            self._calculate_topology()
        # ------------------------------------------------

        src = eth.src
        dst = eth.dst

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
        
        out = parser.OFPPacketOut(
            datapath=datapath, buffer_id=msg.buffer_id,
                                  in_port=in_port, actions=actions, data=msg.data)
        datapath.send_msg(out)

    def _calculate_topology(self):
        """
        Mengambil topologi dari API Standar Ryu dan Hitung Johnson.
        Diakses hanya sekali saat paket pertama masuk.
        """
        try:
            switches = topology_api.get_switch(self, None)
            links = topology_api.get_link(self, None)
            
            self.logger.info(f">>> DISCOVERED: {len(switches)} Switches, {len(links)} Links")
            
            temp_net = nx.DiGraph()
            temp_port_map = {}

            for s in switches:
                temp_net.add_node(s.dp.id)
                if s.dp.id not in temp_port_map: temp_port_map[s.dp.id] = {}

            for l in links:
                temp_net.add_edge(l.src.dpid, l.dst.dpid, port=l.src.port_no, weight=1)
                temp_net.add_edge(l.dst.dpid, l.src.dpid, port=l.dst.port_no, weight=1)
               
                if l.src.dpid in temp_port_map: temp_port_map[l.src.dpid][l.src.port_no] = l.dst.dpid
                if l.dst.dpid in temp_port_map: temp_port_map[l.dst.dpid][l.dst.port_no] = l.src.dpid
            
            # MST
            undirected = temp_net.to_undirected()
            self.mst = nx.minimum_spanning_tree(undirected)

            # JOHNSON ALGORITHM (BERAT - Hanya dijalankan sekali ini)
            self.logger.info(">>> Running JOHNSON ALGORITHM... This may take 30-60s on WSL...")
            self.all_paths = nx.johnson(temp_net, weight='weight')
            
            self.net = temp_net
            self.port_map = temp_port_map
            self.is_ready = True
            
            self.logger.info(">>> TOPOLOGY CALCULATION DONE. SYSTEM READY.")
            self.calc_in_progress = False
           
        except Exception as e:
            self.logger.error(f"Topology Calculation Error: {e}")
            # Jika gagal hitung, biarkan traffic lewat (flood fallback) atau coba lagi nanti
            # Tapi set is_ready True agar tidak deadlock
            self.is_ready = True 