"""An AWS Python Pulumi program"""

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

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
        self.rsw_license = self.config.require("rsw_license")
        self.public_key = self.config.require("public_key")   
        self.pwbServerNumber = self.config.require("pwbServerNumber")
        self.pwbInstanceType = self.config.require("pwbInstanceType")
        self.pwbAmi = self.config.require("pwbAmi")
        self.Domain = self.config.require("Domain")
        self.DomainPW = self.config.require("DomainPW")


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

def make_rsw_server(
    name: str, 
    tags: Dict, 
    key_pair: ec2.KeyPair, 
    vpc_group_ids: List[str],
    instance_type: str,
    ami: str
):
    # Stand up a server.
    server = ec2.Instance(
        f"rstudio-workbench-{name}",
        instance_type=instance_type,
        vpc_security_group_ids=vpc_group_ids,
        ami=ami,
        tags=tags,
        key_name=key_pair.key_name,
        iam_instance_profile="WindowsJoinDomain"
    )
    
    # Export final pulumi variables.
    pulumi.export(f'rsw_{name}_public_ip', server.public_ip)
    pulumi.export(f'rsw_{name}_public_dns', server.public_dns)
    pulumi.export(f'rsw_{name}_subnet_id', server.subnet_id)

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
    rsw_security_group = ec2.SecurityGroup(
        "rsw-ha-sg",
        description="Sam security group for Pulumi deployment",
        ingress=[
            {"protocol": "TCP", "from_port": 22, "to_port": 22, 'cidr_blocks': ['0.0.0.0/0'], "description": "SSH"},
            {"protocol": "TCP", "from_port": 8787, "to_port": 8787, 'cidr_blocks': ['0.0.0.0/0'], "description": "RSW"},
            {"protocol": "TCP", "from_port": 2049, "to_port": 2049, 'cidr_blocks': ['172.31.0.0/16'], "description": "NFS"},
            {"protocol": "TCP", "from_port": 80, "to_port": 80, 'cidr_blocks': ['0.0.0.0/0'], "description": "HTTP"},
            {"protocol": "TCP", "from_port": 5432, "to_port": 5432, 'cidr_blocks': ['172.31.0.0/16'], "description": "POSTGRESQL"},
            {"protocol": "TCP", "from_port": 5559, "to_port": 5559, 'cidr_blocks': ['172.31.0.0/16'], "description": "LAUNCHER"},
            {"protocol": "TCP", "from_port": 32768, "to_port": 60999, 'cidr_blocks': ['172.31.0.0/16'], "description": "LAUNCHER Port"},
        ],
        egress=[
            {"protocol": "All", "from_port": 0, "to_port": 0, 'cidr_blocks': ['0.0.0.0/0'], "description": "Allow all outbout traffic"},
        ],
        tags=tags
    )

    # --------------------------------------------------------------------------
    # Create Target Group for ELB
    # --------------------------------------------------------------------------

    rsw_elb = lb.LoadBalancer(
        "rsw-elb",
        internal=False,
        #security_groups=[rsw_security_group.id],
        subnets=vpc_subnets.ids,
        load_balancer_type="network",
    )
    pulumi.export(f'rsw_elb_dns', rsw_elb.dns_name)

    # --------------------------------------------------------------------------
    # Create Target Group for ELB
    # --------------------------------------------------------------------------

    rsw_tgt_group = lb.TargetGroup("rsw-tgt-group",
        port=80,
        protocol="TCP",
        vpc_id=vpc.id
    )
    
    # --------------------------------------------------------------------------
    # Stand up the servers
    # --------------------------------------------------------------------------
    n_servers=int(config.pwbServerNumber)
    rsw_server=[0]*n_servers

    for i in range(n_servers):
        rsw_server[i] = make_rsw_server(
            str(i+1), 
            tags=tags | {"Name": "rsw-"+str(i+1)},
            key_pair=key_pair,
            vpc_group_ids=[rsw_security_group.id],
            instance_type=config.pwbInstanceType,
	    ami=config.pwbAmi
        )

    # --------------------------------------------------------------------------
    # Add Servers to Target Group
    # --------------------------------------------------------------------------

    for i in range(n_servers):
        lb.TargetGroupAttachment("rsw-tgt-host-"+str(i+1),
             target_group_arn=rsw_tgt_group.arn,
             target_id=rsw_server[i].id, 
	     port="8787"
	)
   
    # --------------------------------------------------------------------------
    # Add Listener to ELB 
    # --------------------------------------------------------------------------

    lb.Listener("rsw-elb-http-listener",
    	load_balancer_arn=rsw_elb.arn,
    	protocol="TCP",
        port=80, 
   	default_actions=[lb.ListenerDefaultActionArgs(
        	type="forward",
        	target_group_arn=rsw_tgt_group.arn,
    	)]
    )

    # --------------------------------------------------------------------------
    # Create EFS.
    # --------------------------------------------------------------------------
    # Create a new file system.
    file_system = efs.FileSystem("rsw-efs",tags= tags | {"Name": "rsw-efs"})
    pulumi.export("efs_id", file_system.id)

    # Create a mount target. Assumes that the servers are on the same subnet id.
    mount_target = efs.MountTarget(
        f"mount-target-rsw",
        file_system_id=file_system.id,
        subnet_id=rsw_server[0].subnet_id,
        security_groups=[rsw_security_group.id]
    )
    
    # --------------------------------------------------------------------------
    # Create a postgresql database.
    # --------------------------------------------------------------------------
    db = rds.Instance(
        "rsw-db",
        instance_class="db.t3.micro",
        allocated_storage=5,
        username="rsw_db_admin",
        password="password",
        db_name="rsw",
        engine="postgres",
        publicly_accessible=True,
        skip_final_snapshot=True,
        tags=tags | {"Name": "rsw-db"},
        vpc_security_group_ids=[rsw_security_group.id]
    )
    pulumi.export("db_port", db.port)
    pulumi.export("db_address", db.address)
    pulumi.export("db_endpoint", db.endpoint)
    pulumi.export("db_name", db.name)
    pulumi.export("db_domain", db.domain)

    # --------------------------------------------------------------------------
    # Create Active Directory
    # --------------------------------------------------------------------------

    available_subnet_id=ec2.get_subnets().ids

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
                available_subnet_id[0],
		available_subnet_id[1],
            ],
        ),
        tags=tags,
    )
    pulumi.export('ad_dns_1', ad.dns_ip_addresses[0])
    pulumi.export('ad_dns_2', ad.dns_ip_addresses[1])
    pulumi.export('ad_access_url', ad.access_url)            
    pulumi.export('ad_domain', ad_domain)
    pulumi.export('ad_passwd', ad_passwd)

    # --------------------------------------------------------------------------
    # Install required software one each server
    # --------------------------------------------------------------------------
    for name, server in zip(range(n_servers), rsw_server):
        connection = remote.ConnectionArgs(
            host=server.public_dns, 
            user="ubuntu", 
            private_key=Path("key.pem").read_text()
        )

        command_set_environment_variables = remote.Command(
            f"server-{name}-set-env", 
            create=pulumi.Output.concat(
                'echo "export EFS_ID=',            file_system.id,           '" >> .env;\n',
                'echo "export AD_PASSWD=',         ad_passwd,   '" >> .env;\n',
                'echo "export AD_DOMAIN=',         ad_domain,   '" >> .env;\n',
                'echo "export NAME=',              str(name+1),        '" >> .env;\n',
                'echo "export RSW_LICENSE=',       os.getenv("RSW_LICENSE"), '" >> .env;',
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
                "server-side-files/config/database.conf",
                "~/database.conf",
                pulumi.Output.all(db.address).apply(lambda x: create_template("server-side-files/config/database.conf").render(db_address=x[0]))
            ),
            serverSideFile(
                "server-side-files/config/load-balancer",
                "~/load-balancer",
                pulumi.Output.all(server.public_ip).apply(lambda x: create_template("server-side-files/config/load-balancer").render(server_ip_address=x[0]))
            ),
            serverSideFile(
                "server-side-files/config/rserver.conf",
                "~/rserver.conf",
                pulumi.Output.all(rsw_elb.dns_name).apply(lambda x: create_template("server-side-files/config/rserver.conf").render(elb_server_dns_name=x[0]))

            ),
            serverSideFile(
                "server-side-files/config/launcher.conf",
                "~/launcher.conf",
                pulumi.Output.all().apply(lambda x: create_template("server-side-files/config/launcher.conf").render())
            ),
            serverSideFile(
                "server-side-files/config/logging.conf",
                "~/logging.conf",
                pulumi.Output.all().apply(lambda x: create_template("server-side-files/config/logging.conf").render())
            ),
            serverSideFile(
                "server-side-files/config/krb5.conf",
                "~/krb5.conf",
                pulumi.Output.all().apply(lambda x: create_template("server-side-files/config/krb5.conf").render(domain_name=ad_domain))
    
            ),
            serverSideFile(
                "server-side-files/config/resolv.conf",
                "~/resolv.conf",
                pulumi.Output.all(ad_domain,ad.dns_ip_addresses).apply(lambda x: create_template("server-side-files/config/resolv.conf").render(domain_name=x[0],dns1=x[1][0], dns2=x[1][1]))
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
            f"server-{name}-build-rsw", 
            # create="alias just='/home/ubuntu/bin/just'; just build-rsw", 
            create="""export PATH="$PATH:$HOME/bin"; just build-rsw""", 
            connection=connection, 
            opts=pulumi.ResourceOptions(depends_on=[command_set_environment_variables, command_install_justfile, command_copy_justfile] + command_copy_config_files)
        )


main()
