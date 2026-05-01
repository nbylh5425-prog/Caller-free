"""Privacy-focused communication infrastructure on AWS.

Deploys:
- VPC with public subnet in us-east-1
- Security groups for WireGuard VPN, FreeSWITCH VoIP (SRTP), and private DNS
- EC2 instance (t3.large, 8GB RAM) bootstrapped with:
  - WireGuard VPN server
  - FreeSWITCH VoIP with SRTP (AES-256 encryption)
  - Unbound DNS resolver with DNS-over-TLS
"""

import pulumi
import pulumi_aws as aws

config = pulumi.Config()
# Allow overriding the SSH key name via config; optional.
ssh_key_name = config.get("sshKeyName")

# ---------------------------------------------------------------------------
# Networking: VPC, Subnet, Internet Gateway, Route Table
# ---------------------------------------------------------------------------

vpc = aws.ec2.Vpc(
    "comm-vpc",
    cidr_block="10.0.0.0/16",
    enable_dns_support=True,
    enable_dns_hostnames=True,
    tags={"Name": "caller-free-vpc"},
)

igw = aws.ec2.InternetGateway(
    "comm-igw",
    vpc_id=vpc.id,
    tags={"Name": "caller-free-igw"},
)

public_subnet = aws.ec2.Subnet(
    "comm-public-subnet",
    vpc_id=vpc.id,
    cidr_block="10.0.1.0/24",
    map_public_ip_on_launch=True,
    availability_zone="us-east-1a",
    tags={"Name": "caller-free-public"},
)

route_table = aws.ec2.RouteTable(
    "comm-rt",
    vpc_id=vpc.id,
    routes=[
        aws.ec2.RouteTableRouteArgs(
            cidr_block="0.0.0.0/0",
            gateway_id=igw.id,
        ),
    ],
    tags={"Name": "caller-free-rt"},
)

aws.ec2.RouteTableAssociation(
    "comm-rta",
    subnet_id=public_subnet.id,
    route_table_id=route_table.id,
)

# ---------------------------------------------------------------------------
# Security Group + Rules (best-practice: separate rule resources)
# ---------------------------------------------------------------------------

sg = aws.ec2.SecurityGroup(
    "comm-sg",
    description="Caller-free communication server security group",
    vpc_id=vpc.id,
    tags={"Name": "caller-free-sg"},
)

# SSH access (restrict to your IP in production)
aws.vpc.SecurityGroupIngressRule(
    "allow-ssh",
    security_group_id=sg.id,
    cidr_ipv4="0.0.0.0/0",
    from_port=22,
    to_port=22,
    ip_protocol="tcp",
    description="SSH access",
)

# WireGuard VPN (UDP 51820)
aws.vpc.SecurityGroupIngressRule(
    "allow-wireguard",
    security_group_id=sg.id,
    cidr_ipv4="0.0.0.0/0",
    from_port=51820,
    to_port=51820,
    ip_protocol="udp",
    description="WireGuard VPN tunnel",
)

# SIP signaling (TCP/UDP 5060-5061 for TLS)
aws.vpc.SecurityGroupIngressRule(
    "allow-sip-udp",
    security_group_id=sg.id,
    cidr_ipv4="0.0.0.0/0",
    from_port=5060,
    to_port=5061,
    ip_protocol="udp",
    description="SIP signaling (UDP)",
)

aws.vpc.SecurityGroupIngressRule(
    "allow-sip-tcp",
    security_group_id=sg.id,
    cidr_ipv4="0.0.0.0/0",
    from_port=5060,
    to_port=5061,
    ip_protocol="tcp",
    description="SIP signaling (TCP/TLS)",
)

# RTP media ports for VoIP (UDP 16384-32767)
aws.vpc.SecurityGroupIngressRule(
    "allow-rtp",
    security_group_id=sg.id,
    cidr_ipv4="0.0.0.0/0",
    from_port=16384,
    to_port=32767,
    ip_protocol="udp",
    description="RTP media (SRTP encrypted voice)",
)

# DNS-over-TLS (TCP 853) - private DNS resolver
aws.vpc.SecurityGroupIngressRule(
    "allow-dns-tls",
    security_group_id=sg.id,
    cidr_ipv4="0.0.0.0/0",
    from_port=853,
    to_port=853,
    ip_protocol="tcp",
    description="DNS-over-TLS (private resolver)",
)

# Allow all outbound traffic
aws.vpc.SecurityGroupEgressRule(
    "allow-all-egress",
    security_group_id=sg.id,
    cidr_ipv4="0.0.0.0/0",
    ip_protocol="-1",
    description="Allow all outbound",
)

# ---------------------------------------------------------------------------
# AMI: Ubuntu 24.04 LTS (Noble Numbat) - latest HVM SSD
# ---------------------------------------------------------------------------

ubuntu_ami = aws.ec2.get_ami(
    most_recent=True,
    filters=[
        {
            "name": "name",
            "values": ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"],
        },
        {
            "name": "virtualization-type",
            "values": ["hvm"],
        },
    ],
    owners=["099720109477"],  # Canonical
)

# ---------------------------------------------------------------------------
# Cloud-init: bootstrap WireGuard, FreeSWITCH, and Unbound
# ---------------------------------------------------------------------------

USER_DATA = """#!/bin/bash
set -euo pipefail
exec > /var/log/caller-free-init.log 2>&1

echo "=== Caller-Free Server Bootstrap ==="

# --- System updates ---
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get upgrade -y

# --- 1. WireGuard VPN Server ---
echo ">>> Installing WireGuard..."
apt-get install -y wireguard qrencode

# Generate server keys
umask 077
wg genkey | tee /etc/wireguard/server_private.key | wg pubkey > /etc/wireguard/server_public.key

SERVER_PRIVKEY=$(cat /etc/wireguard/server_private.key)

cat > /etc/wireguard/wg0.conf <<WGEOF
[Interface]
Address = 10.66.66.1/24
ListenPort = 51820
PrivateKey = ${SERVER_PRIVKEY}

# Enable IP forwarding and NAT
PostUp = sysctl -w net.ipv4.ip_forward=1; iptables -A FORWARD -i wg0 -j ACCEPT; iptables -t nat -A POSTROUTING -o eth0 -j MASQUERADE
PostDown = iptables -D FORWARD -i wg0 -j ACCEPT; iptables -t nat -D POSTROUTING -o eth0 -j MASQUERADE
WGEOF

chmod 600 /etc/wireguard/wg0.conf
systemctl enable --now wg-quick@wg0

echo ">>> WireGuard installed. Server public key:"
cat /etc/wireguard/server_public.key

# --- 2. FreeSWITCH VoIP with SRTP (AES-256) ---
echo ">>> Installing FreeSWITCH..."

# Add FreeSWITCH repository (SignalWire)
apt-get install -y gnupg2 lsb-release apt-transport-https

# Install FreeSWITCH from Ubuntu packages (community build)
apt-get install -y freeswitch freeswitch-mod-commands freeswitch-mod-dptools \
    freeswitch-mod-sofia freeswitch-mod-dialplan-xml freeswitch-mod-event-socket \
    freeswitch-mod-tone-stream freeswitch-mod-sndfile freeswitch-conf-vanilla \
    freeswitch-mod-console || {
    echo ">>> FreeSWITCH packages not in default repos, building from source..."
    apt-get install -y build-essential cmake automake autoconf libtool pkg-config \
        libssl-dev libcurl4-openssl-dev libedit-dev libsqlite3-dev libpcre3-dev \
        libspeexdsp-dev libldns-dev libtiff-dev yasm libopus-dev libsndfile1-dev \
        unzip uuid-dev libavformat-dev libswscale-dev libavresample-dev \
        liblua5.2-dev libpq-dev unixodbc-dev zlib1g-dev libjpeg-dev

    cd /usr/local/src
    git clone --depth 1 -b v1.10 https://github.com/signalwire/freeswitch.git
    cd freeswitch
    git clone https://github.com/signalwire/libks.git
    git clone https://github.com/signalwire/signalwire-c.git

    cd libks && cmake -B build && cmake --build build && cmake --install build && cd ..
    cd signalwire-c && cmake -B build && cmake --build build && cmake --install build && cd ..
    ldconfig

    ./bootstrap.sh -j
    ./configure --enable-core-pgsql-support
    make -j$(nproc)
    make install
    make cd-sounds-install cd-moh-install
    ln -sf /usr/local/freeswitch/bin/freeswitch /usr/bin/freeswitch
    ln -sf /usr/local/freeswitch/bin/fs_cli /usr/bin/fs_cli
}

# Configure SRTP with AES-256 encryption
FREESWITCH_CONF_DIR="/etc/freeswitch"
if [ ! -d "$FREESWITCH_CONF_DIR" ]; then
    FREESWITCH_CONF_DIR="/usr/local/freeswitch/conf"
fi

# Enable SRTP in SIP profiles (force AES-256 encryption)
if [ -f "$FREESWITCH_CONF_DIR/sip_profiles/internal.xml" ]; then
    sed -i 's|<!--<param name="rtp-secure-media" value="true"/>-->|<param name="rtp-secure-media" value="true"/>|g' \
        "$FREESWITCH_CONF_DIR/sip_profiles/internal.xml"
    # Set crypto suite to AES-256
    sed -i '/<param name="rtp-secure-media"/a\\    <param name="rtp-secure-media-suites" value="AEAD_AES_256_GCM_8:AES_256_CM_HMAC_SHA1_80"/>' \
        "$FREESWITCH_CONF_DIR/sip_profiles/internal.xml"
fi

systemctl enable freeswitch 2>/dev/null || true
systemctl start freeswitch 2>/dev/null || true

echo ">>> FreeSWITCH installed with SRTP AES-256."

# --- 3. Unbound DNS Resolver with DNS-over-TLS ---
echo ">>> Installing Unbound DNS resolver..."
apt-get install -y unbound dns-root-data

# Stop systemd-resolved to free port 53
systemctl stop systemd-resolved 2>/dev/null || true
systemctl disable systemd-resolved 2>/dev/null || true

cat > /etc/unbound/unbound.conf.d/caller-free.conf <<UBEOF
server:
    interface: 0.0.0.0
    interface: 0.0.0.0@853
    access-control: 10.66.66.0/24 allow
    access-control: 127.0.0.0/8 allow
    access-control: 0.0.0.0/0 refuse

    # Privacy settings
    hide-identity: yes
    hide-version: yes
    qname-minimisation: yes
    aggressive-nsec: yes
    harden-glue: yes
    harden-dnssec-stripped: yes

    # Performance
    num-threads: 2
    msg-cache-size: 64m
    rrset-cache-size: 128m
    prefetch: yes

    # TLS for incoming queries (DNS-over-TLS on port 853)
    tls-service-key: "/etc/unbound/unbound_server.key"
    tls-service-pem: "/etc/unbound/unbound_server.pem"
    tls-port: 853

    # Use TLS for upstream queries (privacy from ISP)
    forward-zone:
        name: "."
        forward-tls-upstream: yes
        forward-addr: 1.1.1.1@853#cloudflare-dns.com
        forward-addr: 1.0.0.1@853#cloudflare-dns.com
        forward-addr: 9.9.9.9@853#dns.quad9.net
UBEOF

# Generate self-signed TLS cert for DNS-over-TLS
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 \
    -keyout /etc/unbound/unbound_server.key \
    -out /etc/unbound/unbound_server.pem \
    -days 3650 -nodes \
    -subj "/CN=caller-free-dns"

chown unbound:unbound /etc/unbound/unbound_server.key /etc/unbound/unbound_server.pem
chmod 600 /etc/unbound/unbound_server.key

# Point system DNS to local resolver
echo "nameserver 127.0.0.1" > /etc/resolv.conf

systemctl enable --now unbound

echo ">>> Unbound DNS resolver installed with DNS-over-TLS."

# --- 4. Enable IP forwarding permanently ---
echo "net.ipv4.ip_forward = 1" >> /etc/sysctl.d/99-caller-free.conf
sysctl -p /etc/sysctl.d/99-caller-free.conf

echo "=== Caller-Free Server Bootstrap Complete ==="
echo "Services: WireGuard (51820/udp), FreeSWITCH (5060-5061), Unbound DNS (853/tcp)"
"""

# ---------------------------------------------------------------------------
# EC2 Instance: t3.large (8GB RAM) - Communication Hub
# ---------------------------------------------------------------------------

# Build instance args; conditionally include key_name if configured.
instance_args: dict[str, object] = {
    "ami": ubuntu_ami.id,
    "instance_type": aws.ec2.InstanceType.T3_LARGE,
    "subnet_id": public_subnet.id,
    "vpc_security_group_ids": [sg.id],
    "associate_public_ip_address": True,
    "user_data": USER_DATA,
    "user_data_replace_on_change": True,
    "root_block_device": aws.ec2.InstanceRootBlockDeviceArgs(
        volume_size=30,
        volume_type="gp3",
        encrypted=True,
    ),
    "metadata_options": aws.ec2.InstanceMetadataOptionsArgs(
        http_tokens="required",  # IMDSv2 only
        http_endpoint="enabled",
    ),
    "tags": {"Name": "caller-free-hub"},
}

if ssh_key_name:
    instance_args["key_name"] = ssh_key_name

server = aws.ec2.Instance("comm-hub", **instance_args)  # type: ignore[arg-type]

# ---------------------------------------------------------------------------
# Elastic IP for stable public address
# ---------------------------------------------------------------------------

eip = aws.ec2.Eip(
    "comm-eip",
    instance=server.id,
    domain="vpc",
    tags={"Name": "caller-free-eip"},
)

# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

pulumi.export("vpc_id", vpc.id)
pulumi.export("subnet_id", public_subnet.id)
pulumi.export("security_group_id", sg.id)
pulumi.export("instance_id", server.id)
pulumi.export("public_ip", eip.public_ip)
pulumi.export("wireguard_port", 51820)
pulumi.export("sip_ports", "5060-5061")
pulumi.export("dns_tls_port", 853)
pulumi.export(
    "ssh_command",
    eip.public_ip.apply(lambda ip: f"ssh ubuntu@{ip}"),
)
pulumi.export(
    "wireguard_server_pubkey_cmd",
    eip.public_ip.apply(
        lambda ip: f"ssh ubuntu@{ip} cat /etc/wireguard/server_public.key"
    ),
)
