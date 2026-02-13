# Copyright (C) 2011 Nippon Telegraph and Telephone Corporation.
# Licensed under the Apache License, Version 2.0
#
# CONTROLLER MESH - STATIC TOPOLOGY EDITION (100 NODE SAFE)
#
# PERUBAHAN:
# 1. Menghapus EventListener Link (LLDP) untuk menghemat CPU.
# 2. Membaca topology.json yang dibuat oleh Mininet.
# 3. Inactivity Probe dan Boot Phase dikurangi karena data sudah siap.

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, ether_types, arp
from ryu.topology import api as topology_api
from ryu.lib import hub
import networkx as nx
import json
import os
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
        # Kunci gerbang sangat singkat (30 detik) karena data statis sudah ada
        self.initial_lock = 30.0 
        self.is_ready = False
        self.calc_in_progress = False
        self.static_topology_loaded = False
       
        self.logger.info("JohnsonMeshUltraController (Static Mode): Ready.")
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

    # HAPUS EVENT HANDLER LINK DISCOVERY
    # Kita tidak butuh @set_ev_cls([event.EventLinkAdd...]) karena sudah pakai JSON

    def _load_static_topology(self):
        """
        Membaca topology.json dan membangun NetworkX graph.
        """
        if not os.path.exists('topology.json'):
            return False
            
        try:
            self.logger.info("Reading topology.json...")
            with open('topology.json', 'r') as f:
                data = json.load(f)
            
            temp_net = nx.DiGraph()
            temp_port_map = {}
            
            for item in data:
                src_dpid = item['src']
                dst_dpid = item['dst']
                sport = item['sport']
                dport = item['dport']
                
                # Tambah node
                temp_net.add_node(src_dpid)
                temp_net.add_node(dst_dpid)
                
                # Tambah edge (Two way / Bidirectional)
                temp_net.add_edge(src_dpid, dst_dpid, port=sport, weight=1)
                temp_net.add_edge(dst_dpid, src_dpid, port=dport, weight=1)
                
                # Mapping Port
                if src_dpid not in temp_port_map: temp_port_map[src_dpid] = {}
                if dst_dpid not in temp_port_map: temp_port_map[dst_dpid] = {}
                
                temp_port_map[src_dpid][sport] = dst_dpid
                temp_port_map[dst_dpid][dport] = src_dpid
            
            # Simpan ke Global
            self.net = temp_net
            self.port_map = temp_port_map
            
            # Hitung Path (Johnson Algorithm)
            self.all_paths = nx.johnson(self.net, weight='weight')
            
            # Hitung MST (Minimum Spanning Tree) untuk Intelligent Flood
            undirected = self.net.to_undirected()
            if nx.is_connected(undirected):
                self.mst = nx.minimum_spanning_tree(undirected)
            
            self.static_topology_loaded = True
            self.logger.info(">>> STATIC TOPOLOGY LOADED SUCCESSFULLY. SYSTEM READY.")
            return True
        except Exception as e:
            self.logger.error(f"Error loading static topology: {e}")
            return False

    def _monitor_topology(self):
        while True:
            hub.sleep(2.0) # Cek tiap 2 detik
            
            if not self.static_topology_loaded:
                # Coba load file JSON
                if self._load_static_topology():
                    self.is_ready = True # Langsung Buka Gerbang
                else:
                    self.logger.info(">>> Waiting for topology.json file...")
            else:
                # Jika sudah load, kita tidur saja. Tidak perlu monitor.
                # Jika ingin fitur recovery saat file diubah, tambahkan logika di sini.
                pass

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

        # Parsing
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