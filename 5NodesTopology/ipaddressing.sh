
#!/bin/sh

# Example for clab-century-serf topology

for i in $(seq 1 5)
do
  sudo docker exec -d clab-century-serf$i ip link set eth1 up
  sudo docker exec -d clab-century-serf$i ip addr add 10.0.1.$((10+i))/24 brd 10.0.1.255 dev eth1
  sudo docker exec -d clab-century-serf$i ip route del default via 172.20.20.1 dev eth0
  sudo docker exec -d clab-century-serf$i ip route add default via 10.0.1.1 dev eth1
done




