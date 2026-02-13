#!/bin/bash
# Optimasi Kernel Linux untuk SDN Skala Besar (>100 Node)
echo "Optimizing Kernel Parameters..."
sudo sysctl -w net.ipv4.neigh.default.gc_thresh1=1024
sudo sysctl -w net.ipv4.neigh.default.gc_thresh2=2048
sudo sysctl -w net.ipv4.neigh.default.gc_thresh3=4096
sudo sysctl -w fs.file-max=100000
ulimit -n 65535
echo "Cleanup existing Mininet topologies..."
sudo mn -c
echo "Done. Sekarang jalankan Ryu Controller di terminal terpisah."