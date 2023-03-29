#!/bin/bash

user_template() {
cat << EOF

# Entry $3: cn=$1,cn=$2,dc=rsw,dc=posit,dc=co 
dn: cn=$1,cn=$2,dc=rsw,dc=posit,dc=co 
cn: $1
gidnumber: 10000
givenname: $1
homedirectory: /shared/home/$1
loginshell: /bin/bash
mail: $1@example.org
objectclass: inetOrgPerson
objectclass: posixAccount
objectclass: top
sn: $1 
uid: $1
uidnumber: \$(( 10000+$3 ))
userpassword: {MD5}FvEvXoN54ivpleUF6/wbhA==
EOF
}


rm -f users.ldif

for i in `seq 1 25`
do
user_template posit`printf %04i $i` Users $i >> users.ldif
done

ldapadd -h {{ad_dns_1}}  -D "rsw\\Administrator" -w S0perS3cret! < users.ldif

rm -f users.ldif
