# coding:utf8
from azure.mgmt.resource import ResourceManagementClient
from azure.mgmt.network import NetworkManagementClient
from azure.mgmt.compute import ComputeManagementClient
from azure.common.credentials import ServicePrincipalCredentials
import logging
import time
import uuid


logger = logging.getLogger(__name__)

IMAGES = {
    "Debian_12_X64": {
        "display": "Debian 12 Bookworm x64 Gen2",
        "architecture": "x64",
        "sku": "12-gen2",
        "publisher": "Debian",
        "version": "latest",
        "offer": "debian-12",
    },
    "Debian_12_ARM64": {
        "display": "Debian 12 Bookworm ARM64",
        "architecture": "arm64",
        "sku": "12-arm64",
        "publisher": "Debian",
        "version": "latest",
        "offer": "debian-12",
    },
    "Ubuntu_24_04_X64": {
        "display": "Ubuntu Server 24.04 LTS x64 Gen2",
        "architecture": "x64",
        "sku": "server",
        "publisher": "Canonical",
        "version": "latest",
        "offer": "ubuntu-24_04-lts",
    },
    "Ubuntu_24_04_ARM64": {
        "display": "Ubuntu Server 24.04 LTS ARM64 Gen2",
        "architecture": "arm64",
        "sku": "server-arm64",
        "publisher": "Canonical",
        "version": "latest",
        "offer": "ubuntu-24_04-lts",
    },
    "WinData_2022": {
        "display": "Windows Server 2022 Datacenter",
        "architecture": "x64",
        "sku": "2022-Datacenter-smalldisk-g2",
        "publisher": "MicrosoftWindowsServer",
        "version": "latest",
        "offer": "WindowsServer",
    },
}

SIZE_ARCHITECTURES = {
    "Standard_B1s": "x64",
    "Standard_B2ats_v2": "x64",
    "Standard_B2pts_v2": "arm64",
}


def is_size_image_compatible(size, image):
    return SIZE_ARCHITECTURES.get(size) == IMAGES.get(image, {}).get("architecture")


def image_reference(image):
    return {
        key: IMAGES[image][key]
        for key in ("publisher", "offer", "sku", "version")
    }


def create_credential_object(tenant_id, client_id, client_secret):
    print("生成身份证明对象")
    tenant_id = tenant_id
    client_id = client_id
    client_secret = client_secret
    credential = ServicePrincipalCredentials(tenant=tenant_id, client_id=client_id, secret=client_secret)
    return credential


def create_resource_group(subscription_id, credential, tag, location):
    print("Create Resource Group")
    credential = credential
    resource_client = ResourceManagementClient(credential, subscription_id)
    RESOURCE_GROUP_NAME = tag
    LOCATION = location
    rg_result = resource_client.resource_groups.create_or_update(RESOURCE_GROUP_NAME,
                                                                 {
                                                                     "location": LOCATION
                                                                 }
                                                                 )


def create_or_update_vm(subscription_id, credential, tag, location, username, password, size, os, custom, acc, disk, spot):
    compute_client = ComputeManagementClient(credential, subscription_id)
    RESOURCE_GROUP_NAME = tag
    VNET_NAME = ("vnet-" + tag)
    SUBNET_NAME = ("subnet-" + tag)
    IP_NAME = ("ip-" + tag)
    IP_CONFIG_NAME = ("ipconfig-" + tag)
    NIC_NAME = ("nicname-" + tag)
    NSG_NAME = ("nsg-" + tag)
    LOCATION = location
    VM_NAME = tag
    USERNAME = username
    PASSWORD = password
    SIZE = size
    DISK = disk
    CUSTOM = custom
    ACC = acc
    network_client = NetworkManagementClient(credential, subscription_id)
    try:
        print("Create VNET")
        poller = network_client.virtual_networks.create_or_update(RESOURCE_GROUP_NAME,
                                                                  VNET_NAME,
                                                                  {
                                                                      "location": LOCATION,
                                                                      "address_space": {
                                                                          "address_prefixes": ["10.0.0.0/16"]
                                                                      }
                                                                  }
                                                                  )
        vnet_result = poller.result()
        print("Create Subnets")
        poller = network_client.subnets.create_or_update(RESOURCE_GROUP_NAME,
                                                         VNET_NAME, SUBNET_NAME,
                                                         {"address_prefix": "10.0.0.0/24"}
                                                         )
        subnet_result = poller.result()
        print("Create Public IP")
        public_ip_parameters = {
            "location": LOCATION,
            "sku": {"name": "Standard"},
            "public_ip_allocation_method": "Static",
            "public_ip_address_version": "IPV4"
        }
        poller = network_client.public_ip_addresses.create_or_update(RESOURCE_GROUP_NAME, IP_NAME, public_ip_parameters)
        ip_address_result = poller.result()
        print("Create Network Security Group")
        poller = network_client.network_security_groups.create_or_update(
            RESOURCE_GROUP_NAME,
            NSG_NAME,
            {
                "location": LOCATION,
                "security_rules": [
                    {
                        "name": "allow-all-inbound",
                        "protocol": "*",
                        "source_port_range": "*",
                        "destination_port_range": "*",
                        "source_address_prefix": "*",
                        "destination_address_prefix": "*",
                        "access": "Allow",
                        "priority": 100,
                        "direction": "Inbound"
                    },
                    {
                        "name": "allow-all-outbound",
                        "protocol": "*",
                        "source_port_range": "*",
                        "destination_port_range": "*",
                        "source_address_prefix": "*",
                        "destination_address_prefix": "*",
                        "access": "Allow",
                        "priority": 101,
                        "direction": "Outbound"
                    }
                ]
            }
        )
        security_group_result = poller.result()
        print("Create Interface")
        poller = network_client.network_interfaces.create_or_update(RESOURCE_GROUP_NAME,
                                                                    NIC_NAME,
                                                                    {
                                                                        "location": LOCATION,
                                                                        "ip_configurations": [{
                                                                            "name": IP_CONFIG_NAME,
                                                                            "subnet": {"id": subnet_result.id},
                                                                            "public_ip_address": {
                                                                                "id": ip_address_result.id}
                                                                        }],
                                                                        "network_security_group": {
                                                                            "id": security_group_result.id
                                                                        },
                                                                        "enableAcceleratedNetworking": ACC
                                                                    }
                                                                    )
        nic_result = poller.result()
        print("Create VM")
        vm_parameters = {
            "location": LOCATION,
            "storage_profile": {
                "osDisk": {
                    "createOption": "fromImage",
                    "diskSizeGB": DISK
                },
                "image_reference": image_reference(os)
            },
            "hardware_profile": {"vm_size": SIZE},
            "os_profile": {
                "computer_name": VM_NAME,
                "admin_username": USERNAME,
                "admin_password": PASSWORD,
                "customdata": CUSTOM
            },
            "network_profile": {"network_interfaces": [{"id": nic_result.id}]}
        }
        if spot == "True":
            vm_parameters.update({
                "priority": "Spot",
                "evictionPolicy": "Delete",
                "billingProfile": {"maxPrice": -1}
            })
        poller = compute_client.virtual_machines.create_or_update(RESOURCE_GROUP_NAME, VM_NAME, vm_parameters)
        vm_result = poller.result()
        print("Create VM {} successful".format(tag))
    except Exception:
        logger.exception("Create VM %s failed; deleting resource group %s", tag, tag)
        try:
            delete_vm(subscription_id, credential, tag)
        except Exception:
            logger.exception("Failed to delete resource group %s after VM creation failure", tag)
        raise


def start_vm(subscription_id, credential, tag):
    compute_client = ComputeManagementClient(credential, subscription_id)
    GROUP_NAME = tag
    VM_NAME = tag
    async_vm_start = compute_client.virtual_machines.start(
        GROUP_NAME, VM_NAME)
    async_vm_start.wait()


def stop_vm(subscription_id, credential, tag):
    compute_client = ComputeManagementClient(credential, subscription_id)
    GROUP_NAME = tag
    VM_NAME = tag
    async_vm_deallocate = compute_client.virtual_machines.deallocate(
        GROUP_NAME, VM_NAME)
    async_vm_deallocate.wait()


def delete_vm(subscription_id, credential, tag):
    resource_client = ResourceManagementClient(credential, subscription_id)
    GROUP_NAME = tag
    resource_client.resource_groups.delete(GROUP_NAME).result()


def change_ip(subscription_id, credential, tag):
    compute_client = ComputeManagementClient(credential, subscription_id)
    network_client = NetworkManagementClient(credential, subscription_id)
    resource_group_name = tag
    vm = compute_client.virtual_machines.get(resource_group_name, tag)

    try:
        nic_reference = vm.network_profile.network_interfaces[0]
        nic_resource_group, nic_name = resource_id_parts(nic_reference.id)
        nic = network_client.network_interfaces.get(nic_resource_group, nic_name)
        ip_configuration = next(
            configuration for configuration in nic.ip_configurations if configuration.public_ip_address)
        public_ip_resource_group, public_ip_name = resource_id_parts(ip_configuration.public_ip_address.id)
        public_ip = network_client.public_ip_addresses.get(public_ip_resource_group, public_ip_name)
    except (AttributeError, IndexError, StopIteration, ValueError):
        logger.exception("VM %s has no replaceable public IP address", tag)
        raise RuntimeError("未找到可替换的公网 IP")

    allocation_method = str(getattr(public_ip.public_ip_allocation_method, "value",
                                    public_ip.public_ip_allocation_method)).lower()
    if allocation_method == "dynamic":
        logger.info("Changing dynamic public IP for VM %s by deallocating and starting it", tag)
        compute_client.virtual_machines.deallocate(resource_group_name, tag).wait()
        time.sleep(10)
        compute_client.virtual_machines.start(resource_group_name, tag).wait()
        return

    if allocation_method != "static":
        logger.error("VM %s public IP %s uses unsupported allocation method %s", tag, public_ip_name,
                     public_ip.public_ip_allocation_method)
        raise RuntimeError("不支持的公网 IP 分配方式")

    replacement_ip_name = "{}-{}".format(public_ip_name[:71], uuid.uuid4().hex[:8])
    replacement_parameters = {
        "location": public_ip.location,
        "sku": {"name": "Standard"},
        "public_ip_allocation_method": "Static",
        "public_ip_address_version": getattr(public_ip.public_ip_address_version, "value",
                                               public_ip.public_ip_address_version) or "IPV4"
    }
    if public_ip.zones:
        replacement_parameters["zones"] = public_ip.zones

    try:
        logger.info("Creating replacement static public IP %s for VM %s", replacement_ip_name, tag)
        replacement_ip = network_client.public_ip_addresses.create_or_update(
            public_ip_resource_group, replacement_ip_name, replacement_parameters).result()
        ip_configuration.public_ip_address = {"id": replacement_ip.id}
        network_client.network_interfaces.create_or_update(nic_resource_group, nic_name, nic).result()
        network_client.public_ip_addresses.delete(public_ip_resource_group, public_ip_name).result()
        logger.info("Changed static public IP for VM %s", tag)
    except Exception:
        logger.exception("Failed to replace static public IP for VM %s", tag)
        raise


def resource_id_parts(resource_id):
    """从 ARM 资源 ID 中提取资源组和资源名称。"""
    parts = resource_id.strip("/").split("/")
    resource_groups_index = next(index for index, part in enumerate(parts) if part.lower() == "resourcegroups")
    return parts[resource_groups_index + 1], parts[-1]


def list(subscription_id, credential):
    network_client = NetworkManagementClient(credential, subscription_id)
    info = network_client.public_ip_addresses.list_all()
    compute_client = ComputeManagementClient(credential, subscription_id)
    info2 = compute_client.virtual_machines.list_all()
    iplist = []
    taglist = []
    for info in info:
        info = str(info)
        info = str(info).replace("'", "").replace('"', "")
        info = info.split(", ")[-7].split(" ")[1]
        iplist.append(info)
    for info2 in info2:
        info2 = str(info2)
        info2 = str(info2).replace("'", "").replace('"', "")
        info2 = info2.split(", ")[2].split(" ")[1]
        taglist.append(info2)
    dict = {"ip": iplist, "tag": taglist}
    return dict
