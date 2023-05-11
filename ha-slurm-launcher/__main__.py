"""An AWS Python Pulumi program"""

import hashlib
import os,json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List
from functools import reduce
from itertools import chain

import jinja2
import pulumi
from pulumi_aws import ec2, efs, rds, lb, directoryservice
from pulumi_command import remote

# ------------------------------------------------------------------------------
# Helper functions
# ------------------------------------------------------------------------------

@dataclass 
class ConfigValues:
    """A single object to manage all config files."""
    config: pulumi.Config = field(default_factory=lambda: pulumi.Config())
    email: str = field(init=False)
    rsw_license: str = field(init=False)
    public_key: str = field(init=False)

    def __post_init__(self):
        self.email = self.config.require("email")
        self.public_key = self.config.require("public_key")   
        self.slurmVersion = self.config.require("slurmVersion")
        self.slurmHeadNodeServerNumber = self.config.require("slurmHeadNodeServerNumber")
        self.slurmHeadNodeInstanceType = self.config.require("slurmHeadNodeInstanceType")
        self.slurmComputeNodeServerNumber = self.config.require("slurmComputeNodeServerNumber")
        self.slurmComputeNodeInstanceType = self.config.require("slurmComputeNodeInstanceType")
        self.slurmAmi = self.config.require("slurmAmi")
        self.rsw_license = self.config.require("rsw_license")
        self.pwbVersion = self.config.require("pwbVersion")
        self.pwbServerNumber = self.config.require("pwbServerNumber")
        self.pwbInstanceType = self.config.require("pwbInstanceType")
        self.pwbAmi = self.config.require("pwbAmi")
        self.Domain = self.config.require("Domain")
        self.DomainPW = self.config.require("DomainPW")

        self.aws_region = self.config.require("region")

def create_template(path: str) -> jinja2.Template:
    with open(path, 'r') as f:
        template = jinja2.Template(f.read())
    return template


def hash_file(path: str) -> pulumi.Output:
    with open(path, mode="r") as f:
        text = f.read()
    hash_str = hashlib.sha224(bytes(text, encoding='utf-8')).hexdigest()
    return pulumi.Output.concat(hash_str)


# ------------------------------------------------------------------------------
# Infrastructure functions
# ------------------------------------------------------------------------------

def make_server(
    name: str, 
    type: str,
    tags: Dict, 
    key_pair: ec2.KeyPair, 
    vpc_group_ids: List[str],
    subnet_id: str,
    instance_type: str,
    ami: str
):
    # Stand up a server.
    server = ec2.Instance(
        f"{type}-{name}",
        instance_type=instance_type,
        vpc_security_group_ids=vpc_group_ids,
        ami=ami,
        tags=tags,
        subnet_id=subnet_id,
        key_name=key_pair.key_name,
        iam_instance_profile="WindowsJoinDomain"
    )
    
    # Export final pulumi variables.
    pulumi.export(f'{type}_{name}_public_ip', server.public_ip)
    pulumi.export(f'{type}_{name}_public_dns', server.public_dns)

    return server


def main():
    # --------------------------------------------------------------------------
    # Get configuration values
    # --------------------------------------------------------------------------
    config = ConfigValues()

    tags = {
        "rs:environment": "development",
        "rs:owner": config.email,
        "rs:project": "solutions",
    }

    # --------------------------------------------------------------------------
    # Red EC2 instance details
    # --------------------------------------------------------------------------
    ec2_details=json.load(open("tools/ec2-list.json"))

    # --------------------------------------------------------------------------
    # Set up keys.
    # --------------------------------------------------------------------------
    key_pair = ec2.KeyPair(
        "ec2 key pair",
        key_name=f"{config.email}-keypair-for-pulumi",
        public_key=config.public_key,
        tags=tags | {"Name": f"{config.email}-key-pair"},
    )
   
    # --------------------------------------------------------------------------
    # Get VPC information.
    # --------------------------------------------------------------------------
    vpc = ec2.get_vpc(default=True)
    vpc_subnets = ec2.get_subnet_ids(vpc_id=vpc.id)
    vpc_subnet = ec2.get_subnet(id=vpc_subnets.ids[0])
    
 
    # --------------------------------------------------------------------------
    # Make security groups
    # --------------------------------------------------------------------------

    security_group = ec2.SecurityGroup(
        "slurm-sg",
        description="SLURM security group for Pulumi deployment",
        ingress=[
            {"protocol": "TCP", "from_port": 22, "to_port": 22, 
                'cidr_blocks': ['0.0.0.0/0'], "description": "SSH"},
	    {"protocol": "TCP", "from_port": 3000, "to_port": 3000,
                'cidr_blocks': ['0.0.0.0/0'], "description": "Grafana"},
	        {"protocol": "TCP", "from_port": 111, "to_port": 111, 
                'cidr_blocks': [ vpc_subnet.cidr_block ], "description": "Portmapper"},
            {"protocol": "TCP", "from_port": 2049, "to_port": 2049, 
                'cidr_blocks': [ vpc_subnet.cidr_block ], "description": "NFS/EFS"},
            {"protocol": "TCP", "from_port": 8787, "to_port": 8787, 
                'cidr_blocks': ['0.0.0.0/0'], "description": "Posit Workbench Web UI"},
            {"protocol": "TCP", "from_port": 5432, "to_port": 5432, 
                'cidr_blocks': [ vpc_subnet.cidr_block ], "description": "PostgreSQL DB"},
            {"protocol": "TCP", "from_port": 5559, "to_port": 5559, 
                'cidr_blocks': [ vpc_subnet.cidr_block ], "description": "Posit Workbench Launcher"},
            {"protocol": "TCP", "from_port": 6817, "to_port": 6817, 
                'cidr_blocks': [ vpc_subnet.cidr_block ], "description": "SLURM Controller Daemon (slurmctld)"},
            {"protocol": "TCP", "from_port": 6818, "to_port": 6818, 
                'cidr_blocks': [ vpc_subnet.cidr_block ], "description": "SLURM Compute Node Daemon (slurmd)"},
            {"protocol": "TCP", "from_port": 32768, "to_port": 60999, 
                'cidr_blocks': [ vpc_subnet.cidr_block ], "description": "Allow connection on ephemeral ports as defined by /proc/sys/net/ipv4/ip_local_port_range - needed for both SLURM and RStudio IDE sessions"},
	],
        egress=[
            {"protocol": "All", "from_port": 0, "to_port": 0, 
                'cidr_blocks': ['0.0.0.0/0'], "description": "Allow all outbout traffic"},
        ],
        tags=tags
    )

    # --------------------------------------------------------------------------
    # Create ELB for Workbencg
    # --------------------------------------------------------------------------

    workbench_elb = lb.LoadBalancer(
        "workbench-elb",
        internal=False,
        #security_groups=[security_group.id],
        subnets=vpc_subnets.ids,
        load_balancer_type="network",
    )
    pulumi.export(f'workbench_elb_dns', workbench_elb.dns_name)

    # --------------------------------------------------------------------------
    # Create Target Group for Workbench ELB
    # --------------------------------------------------------------------------

    workbench_tgt_group = lb.TargetGroup("workbench-tgt-group",
        port=80,
        protocol="TCP",
        vpc_id=vpc.id
    )

    # --------------------------------------------------------------------------
    # Stand up the servers
    # --------------------------------------------------------------------------
    n_servers=int(config.slurmHeadNodeServerNumber)+int(config.slurmComputeNodeServerNumber)+int(config.pwbServerNumber)
    pulumi.export(f'number_of_servers', n_servers)
    
    # -------------------------------------------------------------------------
    # Head Nodes
    # -------------------------------------------------------------------------

    n_slurm_head_nodes=int(config.slurmHeadNodeServerNumber)
    slurm_head_node=[0]*n_slurm_head_nodes
    pulumi.export(f'number_of_slurm_head_nodes', n_slurm_head_nodes)


    for i in range(n_slurm_head_nodes):
        slurm_head_node[i] = make_server(
            "head-node-"+str(i+1), 
            "slurm",
            tags=tags | {"Name": "slurm-head-node-"+str(i+1)},
            key_pair=key_pair,
            vpc_group_ids=[security_group.id],
            instance_type=config.slurmHeadNodeInstanceType,
            subnet_id=vpc_subnet.id,
	        ami=config.slurmAmi
        )



    # -------------------------------------------------------------------------
    # Compute Nodes
    # -------------------------------------------------------------------------

    n_slurm_compute_nodes=int(config.slurmComputeNodeServerNumber)
    slurm_compute_node=[0]*n_slurm_compute_nodes
    pulumi.export(f'number_of_slurm_compute_nodes', n_slurm_compute_nodes)


    for i in range(n_slurm_compute_nodes):
        slurm_compute_node[i] = make_server(
            "compute-node-"+str(i+1),
            "slurm",
            tags=tags | {"Name": "slurm-compute-node-"+str(i+1)},
            key_pair=key_pair,
            vpc_group_ids=[security_group.id],
            instance_type=config.slurmHeadNodeInstanceType,
            subnet_id=vpc_subnet.id,
            ami=config.slurmAmi
        )

    # -------------------------------------------------------------------------
    # Posit Workbench Servers
    # -------------------------------------------------------------------------

    n_posit_workbench_servers=int(config.pwbServerNumber)
    posit_workbench_server=[0]*n_posit_workbench_servers
    pulumi.export(f'number_of_workbench_servers',n_posit_workbench_servers)


    for i in range(n_posit_workbench_servers):
        posit_workbench_server[i] = make_server(
            "server-"+str(i+1),
            "posit-workbench",
            tags=tags | {"Name": "posit-workbench-server-"+str(i+1)},
            key_pair=key_pair,
            vpc_group_ids=[security_group.id],
            instance_type=config.pwbInstanceType,
            subnet_id=vpc_subnet.id,
            ami=config.pwbAmi
        )

    # --------------------------------------------------------------------------
    # Add Servers to Target Group
    # --------------------------------------------------------------------------

    for i in range(n_posit_workbench_servers):
        lb.TargetGroupAttachment("pwb-tgt-host-"+str(i+1),
             target_group_arn=workbench_tgt_group.arn,
             target_id=posit_workbench_server[i].id, 
             port="8787"
        )
   
    # --------------------------------------------------------------------------
    # Add Listener to ELB 
    # --------------------------------------------------------------------------

    lb.Listener("rsw-elb-http-listener",
        load_balancer_arn=workbench_elb.arn,
        protocol="TCP",
        port=80, 
        default_actions=[lb.ListenerDefaultActionArgs(
                type="forward",
                target_group_arn=workbench_tgt_group.arn,
        )]
    )


    # --------------------------------------------------------------------------
    # Create EFS.
    # --------------------------------------------------------------------------
    # Create a new file system.
    file_system = efs.FileSystem("slurm-efs",tags= tags | {"Name": "slurm-efs"})
    pulumi.export("efs_id", file_system.id)

    # Create a mount target. Assumes that the servers are on the same subnet id.
    mount_target = efs.MountTarget(
        f"mount-target-slurm-1",
        file_system_id=file_system.id,
        subnet_id=vpc_subnets.ids[0],
        security_groups=[security_group.id]
    )

    mount_target = efs.MountTarget(
        f"mount-target-slurm-2",
        file_system_id=file_system.id,
        subnet_id=vpc_subnets.ids[1],
        security_groups=[security_group.id]
    )

    mount_target = efs.MountTarget(
        f"mount-target-slurm-3",
        file_system_id=file_system.id,
        subnet_id=vpc_subnets.ids[2],
        security_groups=[security_group.id]
    )
    


    # --------------------------------------------------------------------------
    # Create a MySQL database for SLURM accounting.
    # --------------------------------------------------------------------------
    slurm_acct_security_group_db = ec2.SecurityGroup(
        "slurm-acct-sg-db",
        description="Security group for EC2 access from SLURM Accounting DB",
        ingress=[
            {"protocol": "TCP", "from_port": 3306, "to_port": 3306, 'cidr_blocks': [ vpc_subnet.cidr_block ], "description": "MySQL"},
        ],
        tags=tags
    )

    slurm_acct_db = rds.Instance(
        "slurm-accounting-db",
        instance_class="db.t3.micro",
        allocated_storage=5,
        username="slurm_acct_admin",
        password="password",
        db_name="slurm_acct",
        engine="mysql",
        engine_version="5.7",
        publicly_accessible=True,
        skip_final_snapshot=True,
        tags=tags | {"Name": "slurm-acct-db"},
        vpc_security_group_ids=[slurm_acct_security_group_db.id]
    )


    #pulumi.export("slurm_acct_db_port", slurm_acct_db.port)
    #pulumi.export("slurm_acct_db_address", slurm_acct_db.address)
    pulumi.export("slurm_acct_db_endpoint", slurm_acct_db.endpoint)
    pulumi.export("slurm_acct_db_name", slurm_acct_db.name)

    # --------------------------------------------------------------------------
    # Create a PostgreSQL database for Posit Workbench.
    # --------------------------------------------------------------------------
    workbench_security_group_db = ec2.SecurityGroup(
        "workbench-acct-sg-db",
        description="Security group for EC2 access from PostgreSQL DB used for Workbench",
        ingress=[
            {"protocol": "TCP", "from_port": 5432, "to_port": 5432, 'cidr_blocks': [ vpc_subnet.cidr_block ], "description": "PostgreSQL"},
        ],
        tags=tags
    )

    workbench_db = rds.Instance(
        "rsw-db",
        instance_class="db.t3.micro",
        allocated_storage=5,
        username="workbench_db_admin",
        password="password",
        db_name="workbench",
        engine="postgres",
        publicly_accessible=True,
        skip_final_snapshot=True,
        tags=tags | {"Name": "rsw-db"},
        vpc_security_group_ids=[workbench_security_group_db.id]
    )
    #pulumi.export("workbench_db_port", workbench_db.port)
    #pulumi.export("workbench_db_address", workbench_db.address)
    pulumi.export("workbench_db_endpoint", workbench_db.endpoint)
    pulumi.export("workbench_db_name", workbench_db.name)


    # --------------------------------------------------------------------------
    # Create Active Directory
    # --------------------------------------------------------------------------

    ad_domain=config.Domain
    ad_passwd=config.DomainPW
    ad = directoryservice.Directory("rsw_directory",
        name=ad_domain,
        password=ad_passwd,
        #edition="Standard",
        type="SimpleAD",
        size="Small",
        description="Directory for RSW environment",
        vpc_settings=directoryservice.DirectoryVpcSettingsArgs(
            vpc_id=vpc.id,
            subnet_ids=[
                vpc_subnets.ids[0],
                vpc_subnets.ids[1],
            ],
        ),
        tags=tags,
    )
    pulumi.export('ad_dns_1', ad.dns_ip_addresses[0])
    pulumi.export('ad_dns_2', ad.dns_ip_addresses[1])
    pulumi.export('ad_access_url', ad.access_url)            
    pulumi.export('ad_domain', ad_domain)
    pulumi.export('ad_passwd', ad_passwd)


    # Install required software one each server
    # --------------------------------------------------------------------------

    ctr=0
    totalinstances=n_slurm_head_nodes+n_slurm_compute_nodes+n_posit_workbench_servers
    command_build=[""]*totalinstances

    for name, server in zip(["slurm_head_node-" + str(n+1) for n in list(range(n_slurm_head_nodes))]+
                                ["slurm_compute_node-" + str(n+1) for n in list(range(n_slurm_compute_nodes))]+
                                ["posit_workbench_server-" + str(n+1) for n in list(range(n_posit_workbench_servers))], 
                                            slurm_head_node+slurm_compute_node+posit_workbench_server):
        connection = remote.ConnectionArgs(
            host=server.public_dns,
            user="ubuntu",
            private_key=Path("key.pem").read_text()
        )

        #remove domain name from private_dns
        slurm_nodes=list(slurm_compute_node[n].private_dns.apply(lambda host: host.split(".")[0])  for n in range(n_slurm_compute_nodes))
        slurm_nodes_out=pulumi.Output.all(slurm_nodes).apply(lambda l: f"{l}")

        #remove domain name from private_dns
        workbench_nodes=list(posit_workbench_server[n].private_dns.apply(lambda host: host.split(".")[0])  for n in range(n_posit_workbench_servers))
        workbench_nodes_out=pulumi.Output.all(workbench_nodes).apply(lambda l: f"{l}")
        
        compute_cpus=pulumi.Output.all(str(ec2_details[config.slurmHeadNodeInstanceType]["vcpus"])).apply(lambda l: f"{l[0]}")
        compute_mem=pulumi.Output.all(str(ec2_details[config.slurmHeadNodeInstanceType]["memory_in_mib"])).apply(lambda l: f"{l[0]}")

        command_set_environment_variables = remote.Command(
            f"{name}-set-env",
            create=pulumi.Output.concat(
                'echo "export EFS_ID=',            file_system.id,           '" > .env;\n',
                'echo "export SLURM_VERSION=',            config.slurmVersion,           '" >> .env;\n',
                'echo "export CIDR_RANGE=',            vpc_subnet.cidr_block,           '" >> .env;\n',
		        'echo "export NFS_SERVER=',            slurm_head_node[0].private_dns.apply(lambda host: host.split(".")[0]),           '" >> .env;\n',
                'echo "export SLURM_SERVERS=',            slurm_head_node[0].private_dns.apply(lambda host: host.split(".")[0]),           '" >> .env;\n',
                'echo "export SLURM_COMPUTE_NODES=\\"',   slurm_nodes_out,           '\\"" >> .env;\n',
                'echo "export SLURM_COMPUTE_NODES_CPU=',  compute_cpus,          '" >> .env;\n',                
                'echo "export SLURM_COMPUTE_NODES_MEM=',   compute_mem,          '" >> .env;\n',
                'echo "export WORKBENCH_NODES=\\"',   workbench_nodes_out,           '\\"" >> .env;\n',
		        'echo "export AD_DOMAIN=', config.Domain, '" >> .env;\n',
                'echo "export AD_PASSWD=', config.DomainPW, '" >> .env;\n',
                'echo "export PWB_VERSION=', config.pwbVersion, '" >> .env;\n',
            ),
            connection=connection,
            opts=pulumi.ResourceOptions(depends_on=[server, slurm_acct_db, workbench_db, file_system, ad])
        )

        command_install_justfile = remote.Command(
            f"{name}-install-justfile",
            create="\n".join([
                """curl --proto '=https' --tlsv1.2 -sSf https://just.systems/install.sh | bash -s -- --to ~/bin;""",
                """echo 'export PATH="$PATH:$HOME/bin"' >> ~/.bashrc;"""
            ]),
            connection=connection,
            opts=pulumi.ResourceOptions(depends_on=[server])
        )

        command_copy_justfile = remote.CopyFile(
            f"{name}-copy-justfile",
            local_path="server-side-files/justfile",
            remote_path='justfile',
            connection=connection,
            opts=pulumi.ResourceOptions(depends_on=[server]),
            triggers=[hash_file("server-side-files/justfile")]
        )

        # Copy the server side files
        @dataclass
        class serverSideFile:
            file_in: str
            file_out: str
            template_render_command: pulumi.Output

        server_side_files=[
            serverSideFile(
                "server-side-files/config/krb5.conf",
                "~/krb5.conf",
                pulumi.Output.all().apply(lambda x: create_template("server-side-files/config/krb5.conf").render(domain_name=ad_domain))
            ),
            serverSideFile(
                "server-side-files/config/resolv.conf",
                "~/resolv.conf",
                pulumi.Output.all(ad_domain,ad.dns_ip_addresses,config.aws_region).apply(lambda x: create_template("server-side-files/config/resolv.conf").render(domain_name=x[0],dns1=x[1][0], dns2=x[1][1], aws_region=x[2]))
            ),
            serverSideFile(
                "server-side-files/config/create-users.exp",
                "~/create-users.exp",
                pulumi.Output.all(ad_domain,ad_passwd).apply(lambda x: create_template("server-side-files/config/create-users.exp").render(domain_name=x[0],domain_passwd=x[1]))
            ),
            serverSideFile(
                "server-side-files/config/create-group.exp",
                "~/create-group.exp",
                pulumi.Output.all(ad_domain,ad_passwd).apply(lambda x: create_template("server-side-files/config/create-group.exp").render(domain_name=x[0],domain_passwd=x[1]))
            ),
            serverSideFile(
                "server-side-files/config/add-group-member.exp",
                "~/add-group-member.exp",
                pulumi.Output.all(ad_domain,ad_passwd).apply(lambda x: create_template("server-side-files/config/add-group-member.exp").render(domain_name=x[0],domain_passwd=x[1]))
            ),

        ]
        
        if "slurm_head_node" in name: 
            server_side_files.append(
                serverSideFile(
                    "server-side-files/config/slurm.conf",
                    "~/slurm.conf",
                    pulumi.Output.all(slurm_head_node[0].private_dns.apply(lambda host: host.split(".")[0]),config.slurmComputeNodeServerNumber).apply(lambda x: create_template("server-side-files/config/slurm.conf").render(slurmctld_host=x[0],compute_nodes=x[1]))
                )
            )
            server_side_files.append(
                serverSideFile( 
                    "server-side-files/config/slurmdbd.conf",
                    "~/slurmdbd.conf",
                    pulumi.Output.all(slurm_acct_db.address,slurm_acct_db.username,slurm_acct_db.password,slurm_acct_db.db_name,slurm_head_node[0].private_dns.apply(lambda host: host.split(".")[0])).apply(lambda x: create_template("server-side-files/config/slurmdbd.conf").render(slurmdb_host=x[0],slurmdb_user=x[1],slurmdb_pass=x[2],slurmdb_name=x[3],slurmdbd_host=x[4]))
                ),
            )

        if "posit_workbench_server-1" in name:
            server_side_files.append(
                serverSideFile(
                    "server-side-files/config/database.conf",
                    "~/database.conf",
                    pulumi.Output.all(workbench_db.address).apply(lambda x: create_template("server-side-files/config/database.conf").render(db_address=x[0]))
                ),
            )
            server_side_files.append(
                serverSideFile(
                    "server-side-files/config/load-balancer",
                    "~/load-balancer",
                    pulumi.Output.all(server.public_ip).apply(lambda x: create_template("server-side-files/config/load-balancer").render(server_ip_address=x[0]))
                ),
            )
            server_side_files.append(
                serverSideFile(
                    "server-side-files/config/rserver.conf",
                    "~/rserver.conf",
                    pulumi.Output.all(workbench_elb.dns_name).apply(lambda x: create_template("server-side-files/config/rserver.conf").render(elb_server_dns_name=x[0]))
                ),
            )
            server_side_files.append(
                serverSideFile(
                    "server-side-files/config/launcher.conf",
                    "~/launcher.conf",
                    pulumi.Output.all().apply(lambda x: create_template("server-side-files/config/launcher.conf").render())
                ),
            )
            server_side_files.append(
                serverSideFile(
                    "server-side-files/config/launcher.slurm.conf",
                    "~/launcher.slurm.conf",
                    pulumi.Output.all().apply(lambda x: create_template("server-side-files/config/launcher.slurm.conf").render())
                ),
            )
            server_side_files.append(
                serverSideFile(
                    "server-side-files/config/launcher.slurm.profiles.conf",
                    "~/launcher.slurm.profiles.conf",
                    pulumi.Output.all().apply(lambda x: create_template("server-side-files/config/launcher.slurm.profiles.conf").render())
                ),
            )
            server_side_files.append(
                serverSideFile(
                    "server-side-files/config/logging.conf",
                    "~/logging.conf",
                    pulumi.Output.all().apply(lambda x: create_template("server-side-files/config/logging.conf").render())
                ),
            )
        

        command_copy_config_files = []
        for f in server_side_files:
            if True:
                command_copy_config_files.append(
                    remote.Command(
                        f"copy {f.file_out} server {name}",
                        create=pulumi.Output.concat('echo "', f.template_render_command, f'" > {f.file_out}'),
                        connection=connection,
                        opts=pulumi.ResourceOptions(depends_on=[server]),
                        triggers=[hash_file(f.file_in)]
                    )
                )

        if "head_node" not in name:
            opts=pulumi.ResourceOptions(depends_on=[command_set_environment_variables, command_install_justfile, command_copy_justfile,  command_build[0]] + command_copy_config_files)
        else:
            opts=pulumi.ResourceOptions(depends_on=[command_set_environment_variables, command_install_justfile, command_copy_justfile] + command_copy_config_files)

        command_build[ctr] = remote.Command(
            f"{name}-do-it",
            # create="alias just='/home/ubuntu/bin/just'; just do-it; just integrate-ad",
            create="""export PATH="$PATH:$HOME/bin"; just do-it; just integrate-ad""",
            connection=connection,
            opts=opts
        )
        ctr=ctr+1

def new_func():
    return 0


main()
