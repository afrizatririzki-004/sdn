import time
import sys
from mininet.net import Mininet
from mininet.node import RemoteController, OVSKernelSwitch
# PENTING: GANTI TCLink KE Link UNTUK KECEPATAN!
from mininet.link import Link 
from mininet.log import setLogLevel, info

def measure_convergence(net, target_host_1, target_host_2, timeout=600):
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

def run_johnson_full_mesh():
    info(f"\n{'='*40}\nJOHNSON FULL MESH - 100 NODES\n{'='*40}\n")
    
    # 1. Inisialisasi (LINK BIASA - BUKAN TCLINK)
    net = Mininet(
        controller=RemoteController,
        switch=OVSKernelSwitch,
        link=Link, # <--- PERUBAHAN KRUSIAL UNTUK KECEPATAN
        autoSetMacs=True,
        build=False
    )

    info(f"*** Adding Controller (Port 6653)...\n")
    net.addController('c0', controller=RemoteController, ip='127.0.0.1', port=6653)

    info(f"*** Adding 100 Switches...\n")
    switches = {}
    for i in range(1, 101):
        switches[i] = net.addSwitch(f's{i}')

    # 2. Membuat TOPOLOGI MATRIX (FULL MESH)
    info(f"*** Creating FULL MATRIX (Johnson Compatible)...\n")
    info(f"*** [WARNING] Membuat 9.900 link (SABAR)... Mohon Tunggu.\n")
    
    link_count = 0
    # Tambahkan print progress agar Anda tahu tidak hang
    for i in range(1, 101):
        for j in range(1, 101):
            if i != j:
                net.addLink(switches[i], switches[j])
                link_count += 1
        
        # Cetak progress setiap 10 switch
        if i % 10 == 0:
            print(f"--- Progress: Created links for {i}/100 switches ({link_count} links) ---")

    info(f"*** Total Link Creation Calls: {link_count} (Expected: 9900)\n")

    # 3. Build & Start
    info(f"*** Building Network (Mungkin butuh beberapa menit)...\n")
    net.build()
    net.start()

    # 4. Konfigurasi OVS
    info(f"*** Setting OpenFlow13 & Probe...\n")
    for sw in net.switches:
        sw.cmd('ovs-vsctl set Bridge {} protocols=OpenFlow13'.format(sw.name))
        sw.cmd('ovs-vsctl set-controller {} tcp:127.0.0.1:6653'.format(sw.name))
        sw.cmd('ovs-vsctl set controller {} inactivity_probe={}'.format(sw.name, 120 * 1000))

    # 5. Menunggu Johnson Lock (Waktu panjang)
    # Karena 100 node, hitungan Johnson bisa butuh waktu lama
    wait_time = 600 # Tunggu 10 menit
    info(f"*** Waiting {wait_time} seconds for Johnson Algorithm & Lock...\n")
    time.sleep(wait_time)

    # 6. Tes
    h_start = net.get('h1')
    h_end = net.get('h100')
    s_fail_1 = 's1'
    s_fail_2 = 's2'

    conv_timeout = 600
    conv_time = measure_convergence(net, h_start, h_end, timeout=conv_timeout)
    
    if conv_time is None:
        th_val = "Skipped (Conv Fail)"
        rec_time = "Skipped (Conv Fail)"
        info(f"*** [ERROR] Convergence GAGAL.\n")
    else:
        info(f"*** Convergence SUKSES: {conv_time:.2f}s\n")
        th_val = measure_throughput(net, h_start, h_end)
        rec_time = measure_recovery(net, s_fail_1, s_fail_2, h_start, h_end)
    
    info(f"\n{'='*40}\nLAPORAN AKHIR: JOHNSON_100_NODES\n{'='*40}\n")
    info(f"Scale           : 100 Nodes\n")
    info(f"Convergence     : {conv_time if conv_time else '> Timeout'}\n")
    info(f"Throughput      : {th_val}\n")
    info(f"Recovery Time   : {rec_time}\n")
    info(f"{'='*40}\n")
    
    net.stop()

if __name__ == '__main__':
    setLogLevel('info')
    run_johnson_full_mesh()