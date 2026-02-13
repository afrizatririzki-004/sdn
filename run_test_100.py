import time
from mininet.net import Mininet
from mininet.node import RemoteController, OVSKernelSwitch
from mininet.link import TCLink
from mininet.log import setLogLevel, info
from skrip_topologi import SkripsiTopo

def measure_convergence(net, target_h1, target_h2, timeout=120):
    info(f"*** [TEST] Mengukur Convergence: {target_h1.name} -> {target_h2.name}...\n")
    start = time.time()
    
    while True:
        result = target_h1.cmd(f'ping -c 1 -W 1 {target_h2.IP()}')
        if "1 received" in result:
            return time.time() - start
        
        if time.time() - start > timeout:
            return None
        time.sleep(1)

def run_experiment():
    info(f"\n{'='*40}\nREACTIVE MODE - MESH (100 Nodes)\n{'='*40}\n")
    
    # Pakai TCLink agar stabil (seperti setup awal yang sukses)
    topo = SkripsiTopo(topo_type='mesh', nodes=100)
    net = Mininet(topo=topo, controller=None, switch=OVSKernelSwitch, link=TCLink)
    
    info(f"*** Adding Controller (Port 6653)...\n")
    c0 = net.addController('c0', controller=RemoteController, ip='127.0.0.1', port=6653)
    
    net.start()
    
    # Set timeout besar
    for sw in net.switches:
        sw.cmd('ovs-vsctl set controller {} inactivity_probe={}'.format(sw.name, 120 * 1000))

    h1 = net.get('h1')
    h_end = net.get('h100')
    
    # Peringatan: Ping pertama mungkin lambat karena Johnson dihitung saat itu terjadi
    info(f"*** Waiting for Network Stability & First Packet...\n")
    time.sleep(60) # Beri waktu 1 menit agar switch connect dan siap menerima paket

    conv_timeout = 300
    conv_time = measure_convergence(net, h1, h_end, timeout=conv_timeout)
    
    if conv_time is None:
        info(f"*** [ERROR] Convergence GAGAL.\n")
    else:
        info(f"*** Convergence SUKSES: {conv_time:.2f}s\n")

    info(f"\n{'='*40}\nLAPORAN AKHIR\n{'='*40}\n")
    info(f"Scale       : 100 Nodes\n")
    info(f"Convergence : {conv_time if conv_time else '> Timeout'}\n")
    info(f"{'='*40}\n")
    
    net.stop()

if __name__ == '__main__':
    setLogLevel('info')
    run_experiment()