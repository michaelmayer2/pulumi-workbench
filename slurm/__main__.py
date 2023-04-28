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
from pulumi_aws import ec2, efs, rds
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

def make_slurm_server(
    name: str, 
    tags: Dict, 
    key_pair: ec2.KeyPair, 
    vpc_group_ids: List[str],
    subnet_id: str,
    instance_type: str,
    ami: str
):
    # Stand up a server.
    server = ec2.Instance(
        f"slurm-{name}",
        instance_type=instance_type,
        vpc_security_group_ids=vpc_group_ids,
        ami=ami,
        tags=tags,
        subnet_id=subnet_id,
        key_name=key_pair.key_name,
        iam_instance_profile="WindowsJoinDomain"
    )
    
    # Export final pulumi variables.
    pulumi.export(f'slurm_{name}_public_ip', server.public_ip)
    pulumi.export(f'slurm_{name}_public_dns', server.public_dns)

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

    slurm_security_group = ec2.SecurityGroup(
        "slurm-sg",
        description="SLURM security group for Pulumi deployment",
        ingress=[
            {"protocol": "TCP", "from_port": 22, "to_port": 22, 'cidr_blocks': ['0.0.0.0/0'], "description": "SSH"},
	        {"protocol": "TCP", "from_port": 111, "to_port": 111, 'cidr_blocks': [ vpc_subnet.cidr_block ], "description": "portmapper"},
            {"protocol": "TCP", "from_port": 2049, "to_port": 2049, 'cidr_blocks': [ vpc_subnet.cidr_block ], "description": "NFS/EFS"},
            {"protocol": "TCP", "from_port": 6817, "to_port": 6817, 'cidr_blocks': [ vpc_subnet.cidr_block ], "description": "slurmctld"},
            {"protocol": "TCP", "from_port": 6818, "to_port": 6818, 'cidr_blocks': [ vpc_subnet.cidr_block ], "description": "slurmd"},
            #{"protocol": "All", "from_port": 0, "to_port": 0, 'cidr_blocks': [ vpc_subnet.cidr_block ], "description": "Allow all inbound traffic from subnet"},
	],
        egress=[
            {"protocol": "All", "from_port": 0, "to_port": 0, 'cidr_blocks': ['0.0.0.0/0'], "description": "Allow all outbout traffic"},
        ],
        tags=tags
    )


    # --------------------------------------------------------------------------
    # Stand up the servers
    # --------------------------------------------------------------------------
    n_servers=int(config.slurmHeadNodeServerNumber)+int(config.slurmComputeNodeServerNumber)
    pulumi.export(f'number_of_slurm_servers', n_servers)
    slurm_server=[0]*n_servers
    
    # -------------------------------------------------------------------------
    # Head Nodes
    # -------------------------------------------------------------------------

    n_slurm_head_nodes=int(config.slurmHeadNodeServerNumber)
    slurm_head_node=[0]*n_slurm_head_nodes
    pulumi.export(f'number_of_slurm_head_nodes', n_slurm_head_nodes)


    for i in range(n_slurm_head_nodes):
        slurm_head_node[i] = make_slurm_server(
            "head-node-"+str(i+1), 
            tags=tags | {"Name": "slurm-head-node-"+str(i+1)},
            key_pair=key_pair,
            vpc_group_ids=[slurm_security_group.id],
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
        slurm_compute_node[i] = make_slurm_server(
            "compute-node-"+str(i+1),
            tags=tags | {"Name": "slurm-compute-node-"+str(i+1)},
            key_pair=key_pair,
            vpc_group_ids=[slurm_security_group.id],
            instance_type=config.slurmHeadNodeInstanceType,
            subnet_id=vpc_subnet.id,
            ami=config.slurmAmi
        )



    # --------------------------------------------------------------------------
    # Create EFS.
    # --------------------------------------------------------------------------
    # Create a new file system.
    file_system = efs.FileSystem("slurm-efs",tags= tags | {"Name": "slurm-efs"})
    pulumi.export("efs_id", file_system.id)

    # Create a mount target. Assumes that the servers are on the same subnet id.
    mount_target = efs.MountTarget(
        f"mount-target-slurm",
        file_system_id=file_system.id,
        subnet_id=slurm_head_node[0].subnet_id,
        security_groups=[slurm_security_group.id]
    )
    
    # --------------------------------------------------------------------------
    # Create a postgresql database.
    # --------------------------------------------------------------------------
    slurm_acct_security_group_db = ec2.SecurityGroup(
        "slurm-acct-sg-db",
        description="SLURM security group for EC2 access from Accounting DB",
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


    pulumi.export("slurm_acct_db_port", slurm_acct_db.port)
    pulumi.export("slurm_acct_db_address", slurm_acct_db.address)
    pulumi.export("slurm_acct_db_endpoint", slurm_acct_db.endpoint)
    pulumi.export("slurm_acct_db_name", slurm_acct_db.name)

    # Install required software one each server
    # --------------------------------------------------------------------------

    ctr=0
    totalinstances=n_slurm_head_nodes+n_slurm_compute_nodes
    command_build_slurm=[""]*totalinstances

    for name, server in zip(["slurm_head_node-" + str(n+1) for n in list(range(n_slurm_head_nodes))]+["slurm_compute_node-" + str(n+1) for n in list(range(n_slurm_compute_nodes))], 
                                            slurm_head_node+slurm_compute_node):
        connection = remote.ConnectionArgs(
            host=server.public_dns,
            user="ubuntu",
            private_key=Path("key.pem").read_text()
        )

        #remove domain name from private_dns
        slurm_nodes=list(slurm_compute_node[n].private_dns.apply(lambda host: host.split(".")[0])  for n in range(n_slurm_compute_nodes))
        newtest=pulumi.Output.all(slurm_nodes).apply(lambda l: f"{l}")
        
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
                'echo "export SLURM_COMPUTE_NODES=\\"',   newtest,           '\\"" >> .env;\n',
                'echo "export SLURM_COMPUTE_NODES_CPU=',  compute_cpus,          '" >> .env;\n',                
                'echo "export SLURM_COMPUTE_NODES_MEM=',   compute_mem,          '" >> .env;\n',
            ),
            connection=connection,
            opts=pulumi.ResourceOptions(depends_on=[server, slurm_acct_db, file_system])
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
            f"{name}--copy-justfile",
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

        if "slurm_head_node" in name: 
            server_side_files = [
                serverSideFile(
                    "server-side-files/config/slurm.conf",
                    "~/slurm.conf",
                    pulumi.Output.all(slurm_head_node[0].private_dns.apply(lambda host: host.split(".")[0]),config.slurmComputeNodeServerNumber).apply(lambda x: create_template("server-side-files/config/slurm.conf").render(slurmctld_host=x[0],compute_nodes=x[1]))
                ),
                serverSideFile( 
                    "server-side-files/config/slurmdbd.conf",
                    "~/slurmdbd.conf",
                    pulumi.Output.all(slurm_acct_db.address,slurm_acct_db.username,slurm_acct_db.password,slurm_acct_db.db_name,slurm_head_node[0].private_dns.apply(lambda host: host.split(".")[0])).apply(lambda x: create_template("server-side-files/config/slurmdbd.conf").render(slurmdb_host=x[0],slurmdb_user=x[1],slurmdb_pass=x[2],slurmdb_name=x[3],slurmdbd_host=x[4]))
                ),
            ]


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
            opts=pulumi.ResourceOptions(depends_on=[command_set_environment_variables, command_install_justfile, command_copy_justfile,  command_build_slurm[0]] + command_copy_config_files)
        else:
            opts=pulumi.ResourceOptions(depends_on=[command_set_environment_variables, command_install_justfile, command_copy_justfile] + command_copy_config_files)

        command_build_slurm[ctr] = remote.Command(
            f"{name}-do-it",
            # create="alias just='/home/ubuntu/bin/just'; just do-it",
            create="""export PATH="$PATH:$HOME/bin"; just do-it""",
            connection=connection,
            opts=opts
        )
        ctr=ctr+1

def new_func():
    return 0


main()
