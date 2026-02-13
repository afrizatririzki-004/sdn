# JOHNSON MESH FULL MATRIX
# Disesuaikan agar memenuhi asumsi TARGET_LINKS = 9900 (Directed)
# pada controller JohnsonMeshUltraController.

from mininet.net import Mininet
from mininet.node import RemoteController, OVSKernelSwitch
from mininet.link import TCLink
from mininet.log import setLogLevel

def johnson_full_mesh():
    net = Mininet(
        controller=RemoteController,
        switch=OVSKernelSwitch,
        link=TCLink, # Tetap TCLink sesuai konteks awal
        autoSetMacs=True,
        build=False
    )

    print("*** Adding controller")
    net.addController(
        'c0',
        controller=RemoteController,
        ip='127.0.0.1',
        port=6653
    )

    print("*** Adding switches")
    switches = {}
    for i in range(1, 101):
        switches[i] = net.addSwitch(f's{i}')

    # --- PERUBAHAN UTAMA: FULL MATRIX LOOP ---
    # Kita buat link di kedua arah (i->j dan j->i)
    # untuk setiap pasang i!=j.
    # Hasil: 100 * 99 = 9900 Link.
    print("*** Creating FULL MATRIX MESH (Johnson Compatible)...")
    for i in range(1, 101):
        for j in range(1, 101):
            if i != j:
                # Menambahkan link dua arah secara fisik di Mininet
                net.addLink(switches[i], switches[j])
    # ------------------------------------------

    print("*** Building & starting network")
    net.build()
    net.start()

    print("*** Network ready (WAIT for Johnson LOCK)")
    # Jangan stop secara otomatis, biarkan Anda bisa tes ping
    # net.stop() 

if __name__ == '__main__':
    setLogLevel('info')
    johnson_full_mesh()