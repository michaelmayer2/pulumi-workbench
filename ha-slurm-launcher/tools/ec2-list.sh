#!/bin/bash

aws ec2 describe-instance-types --filters Name=current-generation,Values=true | jq -c '.InstanceTypes | reduce .[] as $i ({}; .[$i.InstanceType] = { memory_in_mib: $i.MemoryInfo.SizeInMiB, vcpus: $i.VCpuInfo.DefaultVCpus})' | jq > ec2-list.csv
