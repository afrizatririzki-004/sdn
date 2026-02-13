import time
import json
import sys
from mininet.net import Mininet
from mininet.node import RemoteController, OVSKernelSwitch
from mininet.link import Link 
from mininet.log import setLogLevel, info
from mininet.node import Host

try:
    from skrip_topologi import SkripsiTopo
except ImportError:
    print("ERROR: File 'skrip_topologi.py' tidak ditemukan!")
    sys.exit(1)

def set_ovs_protocol_and_timeout(net, timeout=120):
    info(f"*** [CONFIG] Setting OpenFlow13 & Inactivity Probe to {timeout}s...\n")
    for sw in net.switches:
        sw.cmd('ovs-vsctl set Bridge {} protocols=OpenFlow13'.format(sw.name))
        sw.cmd('ovs-vsctl set-controller {} tcp:127.0.0.1:6653'.format(sw.name))
        sw.cmd('ovs-vsctl set controller {} inactivity_probe={}'.format(sw.name, timeout * 1000))

def measure_convergence(net, target_host_1, target_host_2, timeout=120):
    info(f"*** [TEST] Mengukur Convergence Time: {target_host_1.name} -> {target_host_2.name}...\n")
    start_time = time.time()
    
    while True:
        result = target_host_1.cmd(f'ping -c 1 -W 1 {target_host_2.IP()}')
        if "1 received" in result:
            return time.time() - start_time
        
        if time.time() - start_time > timeout:
            return None
        time.sleep(0.5)

def measure_throughput(net, client, server):
    info(f"*** [TEST] Mengukur Throughput...\n")
    server.cmd('killall -9 iperf')
    client.cmd('killall -9 iperf')
    time.sleep(0.5)
    
    server.cmd('iperf -s &')
    time.sleep(1) 
    
    iperf_output = client.cmd(f'iperf -c {server.IP()} -t 5 -f m')
    
    try:
        lines = iperf_output.split('\n')
        result_line = [l for l in lines if 'Mbits/sec' in l][-1]
        throughput_val = result_line.split()[-2] + " " + result_line.split()[-1]
    except:
        throughput_val = "Error"
    
    server.cmd('killall -9 iperf')
    client.cmd('killall -9 iperf')
    return throughput_val

def measure_recovery(net, s_src, s_dst, h_src, h_dst):
    info(f"*** [TEST] Mengukur Recovery Time (Link {s_src}-{s_dst} DOWN)...\n")
    
    h_src.cmd(f'ping -c 1 {h_dst.IP()}')
    h_src.cmd(f'ping -i 0.2 {h_dst.IP()} > /dev/null 2>&1 &')
    
    time.sleep(2) 
    
    info(f"*** [ACTION] Matikan Link {s_src}-{s_dst}\n")
    start_fail_time = time.time()
    net.configLinkStatus(s_src, s_dst, 'down')
    
    recovered = False
    max_wait = 120 
    
    while time.time() - start_fail_time < max_wait:
        res = h_src.cmd(f'ping -c 1 -W 1 {h_dst.IP()}')
        if "1 received" in res:
            recovered = True
            break
        time.sleep(0.2)
        
    h_src.cmd('killall -9 ping')
    net.configLinkStatus(s_src, s_dst, 'up')
    
    if not recovered: return f"> {max_wait}s (Fail)"
    return f"{(time.time() - start_fail_time):.2f}s"

def run_mesh_test(nodes_or_k, algo_name="RAW_MODE"):
    info(f"\n{'='*40}\nSTARTING: {algo_name} - MESH ({nodes_or_k} Nodes)\n{'='*40}\n")
    
    topo = SkripsiTopo(topo_type='mesh', nodes=nodes_or_k)
    net = Mininet(topo=topo, controller=None, switch=OVSKernelSwitch, link=Link)
    
    info(f"*** Adding Controller (Remote)...\n")
    c0 = net.addController('c0', controller=RemoteController, ip='127.0.0.1', port=6653)
    
    net.start()
    set_ovs_protocol_and_timeout(net, timeout=120)
    
    # --- GENERATE STATIC TOPOLOGY FILE (JSON) ---
    info(f"*** [PRE-CONFIG] Generating topology.json...\n")
    link_data = []
    
    for link in net.links:
        # FIX HANYA: Simpan link jika kedua ujungnya adalah SWITCH
        # Abaikan link Host-Switch, agar Controller tidak membuat Self-Loop
        if isinstance(link.intf1.node, OVSKernelSwitch) and isinstance(link.intf2.node, OVSKernelSwitch):
            try:
                s_name = link.intf1.node.name 
                d_name = link.intf2.node.name 
                src_dpid = int(s_name[1:]) 
                dst_dpid = int(d_name[1:])
                sport = link.intf1.node.ports[link.intf1]
                dport = link.intf2.node.ports[link.intf2]
                
                link_data.append({
                    'src': src_dpid,
                    'dst': dst_dpid,
                    'sport': sport,
                    'dport': dport
                })
            except Exception as e:
                pass

    with open('topology.json', 'w') as f:
        json.dump(link_data, f)
    
    info(f"*** [INFO] Topology saved! Total Switch-to-Switch Links: {len(link_data)}\n")
    # ---------------------------------------------

    # TUNGGU CONTROLLER MEMBACA
    wait_time = 20 
    info(f"*** Waiting {wait_time} seconds for Controller to Load Static Map...\n")
    time.sleep(wait_time)
    
    # --- FITUR BARU: WARM UP ---
    # Kita lakukan pingAll dulu untuk memicu ARP Storm agar selesai sebelum tes asli.
    # Kita abaikan hasilnya, fokus memanaskan switch.
    info(f"*** [WARM UP] Flushing ARP Storm (pingAll)...\n")
    net.pingAll(timeout=1) 
    time.sleep(10) # Tunggu 10 detik agar Ryu selesai memproses ARP ini.
    # ----------------------------------------

    h_start = net.get('h1')
    h_end = net.get(f'h{nodes_or_k}')
    s_fail_1 = 's1'
    s_fail_2 = 's2'

    conv_timeout = 120
    conv_time = measure_convergence(net, h_start, h_end, timeout=conv_timeout)
    
    if conv_time is None:
        th_val = "Skipped (Conv Fail)"
        rec_time = "Skipped (Conv Fail)"
        info(f"*** [ERROR] Convergence GAGAL.\n")
    else:
        info(f"*** Convergence SUKSES: {conv_time:.2f}s\n")
        th_val = measure_throughput(net, h_start, h_end)
        rec_time = measure_recovery(net, s_fail_1, s_fail_2, h_start, h_end)
    
    info(f"\n{'='*40}\nLAPORAN AKHIR: {algo_name}\n{'='*40}\n")
    info(f"Scale           : {nodes_or_k} Nodes\n")
    info(f"Convergence     : {conv_time if conv_time else '> Timeout'}\n")
    info(f"Throughput      : {th_val}\n")
    info(f"Recovery Time   : {rec_time}\n")
    info(f"{'='*40}\n")
    
    net.stop()

if __name__ == '__main__':
    setLogLevel('info')
    run_mesh_test(100)