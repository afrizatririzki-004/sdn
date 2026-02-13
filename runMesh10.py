import time
import sys
from functools import partial
from mininet.net import Mininet
from mininet.node import RemoteController, OVSKernelSwitch
from mininet.link import TCLink
from mininet.log import setLogLevel, info

# PASTIKAN FILE SKRIP TOPOLOGI ADA
try:
    from skrip_topologi import SkripsiTopo
except ImportError:
    print("ERROR: File 'skrip_topologi.py' tidak ditemukan!")
    sys.exit(1)

def set_ovs_protocol_and_timeout(net, timeout=60):
    info(f"*** [CONFIG] Setting OpenFlow13 & Inactivity Probe to {timeout}s...\n")
    for sw in net.switches:
        sw.cmd('ovs-vsctl set Bridge {} protocols=OpenFlow13'.format(sw.name))
        sw.cmd('ovs-vsctl set-controller {} tcp:127.0.0.1:6653'.format(sw.name))
        sw.cmd('ovs-vsctl set controller {} inactivity_probe={}'.format(sw.name, timeout * 1000))

def measure_convergence(net, target_host_1, target_host_2, timeout=300):
    info(f"*** [TEST] Mengukur Convergence Time: {target_host_1.name} -> {target_host_2.name}...\n")
    start_time = time.time()
    
    while True:
        result = target_host_1.cmd(f'ping -c 1 -W 1 {target_host_2.IP()}')
        if "1 received" in result:
            return time.time() - start_time
        
        if time.time() - start_time > timeout:
            return None
        time.sleep(1)

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

def run_mesh_test(nodes_or_k=10, algo_name="JOHNSON_10_NODES"):
    info(f"\n{'='*40}\nSTARTING: {algo_name} - MESH ({nodes_or_k} Nodes)\n{'='*40}\n")
    
    topo = SkripsiTopo(topo_type='mesh', nodes=nodes_or_k)
    net = Mininet(topo=topo, controller=None, switch=OVSKernelSwitch, link=TCLink)
    
    info(f"*** Adding Controller (Remote)...\n")
    c0 = net.addController('c0', controller=RemoteController, ip='127.0.0.1', port=6653)
    
    net.start()
    set_ovs_protocol_and_timeout(net, timeout=60)
    
    # UPDATE: Wait Time pendek untuk 10 Node
    # Kita lock controller 60 detik, jadi tunggu 90 detik (30 detik safety)
    wait_time = 90
    
    info(f"*** Waiting {wait_time} seconds for Topology Stability & Calculation...\n")
    time.sleep(wait_time)
    
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
    # Hardcode 10 nodes untuk file ini sesuai request
    run_mesh_test(10)