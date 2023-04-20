"""An AWS Python Pulumi program"""

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

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
        key_name=key_pair.key_name,
        iam_instance_profile="WindowsJoinDomain"
    )
    
    # Export final pulumi variables.
    pulumi.export(f'slurm_{name}_public_ip', server.public_ip)
    pulumi.export(f'slurm_{name}_public_dns', server.public_dns)
    pulumi.export(f'slurm_{name}_subnet_id', server.subnet_id)

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

 
    # --------------------------------------------------------------------------
    # Make security groups
    # --------------------------------------------------------------------------
    slurm_security_group = ec2.SecurityGroup(
        "slurm-sg",
        description="SLURM security group for Pulumi deployment",
        ingress=[
            {"protocol": "TCP", "from_port": 22, "to_port": 22, 'cidr_blocks': ['0.0.0.0/0'], "description": "SSH"},
	    {"protocol": "TCP", "from_port": 2049, "to_port": 2049, 'cidr_blocks': ['172.31.0.0/16'], "description": "NFS"},
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
    db = rds.Instance(
        "slurm-accounting-db",
        instance_class="db.t3.micro",
        allocated_storage=5,
        username="slurm_acct_admin",
        password="password",
        db_name="slurm",
        engine="mysql",
        engine_version="5.7",
        publicly_accessible=True,
        skip_final_snapshot=True,
        tags=tags | {"Name": "slurm-acct-db"},
        vpc_security_group_ids=[slurm_security_group.id]
    )
    pulumi.export("slurm_acct_db_port", db.port)
    pulumi.export("slurm_acct_db_address", db.address)
    pulumi.export("slurm_acct_db_endpoint", db.endpoint)
    pulumi.export("slurm_acct_db_name", db.name)


    # Install required software one each server
    # --------------------------------------------------------------------------
    for name, server in zip(range(n_slurm_head_nodes), slurm_head_node):
        connection = remote.ConnectionArgs(
            host=server.public_dns,
            user="ubuntu",
            private_key=Path("key.pem").read_text()
        )

        command_set_environment_variables = remote.Command(
            f"server-{name}-set-env",
            create=pulumi.Output.concat(
                'echo "export EFS_ID=',            file_system.id,           '" >> .env;\n',
                'echo "export SLURM_VERSION=',            config.slurmVersion,           '" >> .env;\n',
            ),
            connection=connection,
            opts=pulumi.ResourceOptions(depends_on=[server, db, file_system])
        )

        command_install_justfile = remote.Command(
            f"server-{name}-install-justfile",
            create="\n".join([
                """curl --proto '=https' --tlsv1.2 -sSf https://just.systems/install.sh | bash -s -- --to ~/bin;""",
                """echo 'export PATH="$PATH:$HOME/bin"' >> ~/.bashrc;"""
            ]),
            connection=connection,
            opts=pulumi.ResourceOptions(depends_on=[server])
        )

        command_copy_justfile = remote.CopyFile(
            f"server-{name}-copy-justfile",
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

        server_side_files = [
            serverSideFile(
                "server-side-files/config/slurm.conf",
                "~/slurm.conf",
                pulumi.Output.all(db.address).apply(lambda x: create_template("server-side-files/config/slurm.conf").render(db_address=x[0]))
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

        command_build_rsw = remote.Command(
            f"server-{name}-build-slurm-head-node",
            # create="alias just='/home/ubuntu/bin/just'; just build-slurm",
            create="""export PATH="$PATH:$HOME/bin"; just build-slurm""",
            connection=connection,
            opts=pulumi.ResourceOptions(depends_on=[command_set_environment_variables, command_install_justfile, command_copy_justfile] + command_copy_config_files)
        )


main()
