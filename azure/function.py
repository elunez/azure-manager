# coding:utf8
from azure.mgmt.resource import ResourceManagementClient
from azure.mgmt.network import NetworkManagementClient
from azure.mgmt.compute import ComputeManagementClient
from azure.identity import ClientSecretCredential
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
    return ClientSecretCredential(
        tenant_id=tenant_id,
        client_id=client_id,
        client_secret=client_secret,
    )


def validate_credential(subscription_id, credential):
    """通过一次只读请求验证身份信息和订阅访问权限。"""
    resource_client = ResourceManagementClient(credential, subscription_id)
    next(iter(resource_client.resource_groups.list()), None)


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


def create_or_update_vm(subscription_id, credential, tag, location, username, password, size, os, custom, disk):
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
    network_client = NetworkManagementClient(credential, subscription_id)
    try:
        print("Create VNET")
        poller = network_client.virtual_networks.begin_create_or_update(RESOURCE_GROUP_NAME,
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
        poller = network_client.subnets.begin_create_or_update(RESOURCE_GROUP_NAME,
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
        poller = network_client.public_ip_addresses.begin_create_or_update(
            RESOURCE_GROUP_NAME, IP_NAME, public_ip_parameters)
        ip_address_result = poller.result()
        print("Create Network Security Group")
        poller = network_client.network_security_groups.begin_create_or_update(
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
        poller = network_client.network_interfaces.begin_create_or_update(RESOURCE_GROUP_NAME,
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
                                                                              }
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
        poller = compute_client.virtual_machines.begin_create_or_update(
            RESOURCE_GROUP_NAME, VM_NAME, vm_parameters)
        vm_result = poller.result()
        print("Create VM {} successful".format(tag))
    except Exception:
        logger.exception("Create VM %s failed; deleting resource group %s", tag, tag)
        try:
            delete_vm(subscription_id, credential, tag)
        except Exception:
            logger.exception("Failed to delete resource group %s after VM creation failure", tag)
        raise


def start_vm(subscription_id, credential, resource_group, vm_name):
    compute_client = ComputeManagementClient(credential, subscription_id)
    async_vm_start = compute_client.virtual_machines.begin_start(resource_group, vm_name)
    async_vm_start.wait()


def stop_vm(subscription_id, credential, resource_group, vm_name):
    compute_client = ComputeManagementClient(credential, subscription_id)
    async_vm_deallocate = compute_client.virtual_machines.begin_deallocate(resource_group, vm_name)
    async_vm_deallocate.wait()


def delete_vm(subscription_id, credential, resource_group):
    resource_client = ResourceManagementClient(credential, subscription_id)
    resource_client.resource_groups.begin_delete(resource_group).result()


def change_ip(subscription_id, credential, resource_group_name, vm_name):
    compute_client = ComputeManagementClient(credential, subscription_id)
    network_client = NetworkManagementClient(credential, subscription_id)
    vm = compute_client.virtual_machines.get(resource_group_name, vm_name)

    try:
        nic_reference = vm.network_profile.network_interfaces[0]
        nic_resource_group, nic_name = resource_id_parts(nic_reference.id)
        nic = network_client.network_interfaces.get(nic_resource_group, nic_name)
        ip_configuration = next(
            configuration for configuration in nic.ip_configurations if configuration.public_ip_address)
        public_ip_resource_group, public_ip_name = resource_id_parts(ip_configuration.public_ip_address.id)
        public_ip = network_client.public_ip_addresses.get(public_ip_resource_group, public_ip_name)
    except (AttributeError, IndexError, StopIteration, ValueError):
        logger.exception("VM %s/%s has no replaceable public IP address", resource_group_name, vm_name)
        raise RuntimeError("未找到可替换的公网 IP")

    allocation_method = str(getattr(public_ip.public_ip_allocation_method, "value",
                                    public_ip.public_ip_allocation_method)).lower()
    if allocation_method == "dynamic":
        logger.info("Changing dynamic public IP for VM %s/%s by deallocating and starting it",
                    resource_group_name, vm_name)
        compute_client.virtual_machines.begin_deallocate(resource_group_name, vm_name).wait()
        time.sleep(10)
        compute_client.virtual_machines.begin_start(resource_group_name, vm_name).wait()
        return

    if allocation_method != "static":
        logger.error("VM %s/%s public IP %s uses unsupported allocation method %s",
                     resource_group_name, vm_name, public_ip_name, public_ip.public_ip_allocation_method)
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
        logger.info("Creating replacement static public IP %s for VM %s/%s",
                    replacement_ip_name, resource_group_name, vm_name)
        replacement_ip = network_client.public_ip_addresses.begin_create_or_update(
            public_ip_resource_group, replacement_ip_name, replacement_parameters).result()
        ip_configuration.public_ip_address = {"id": replacement_ip.id}
        network_client.network_interfaces.begin_create_or_update(
            nic_resource_group, nic_name, nic).result()
        network_client.public_ip_addresses.begin_delete(
            public_ip_resource_group, public_ip_name).result()
        logger.info("Changed static public IP for VM %s/%s", resource_group_name, vm_name)
    except Exception:
        logger.exception("Failed to replace static public IP for VM %s/%s", resource_group_name, vm_name)
        raise


def resource_id_parts(resource_id):
    """从 ARM 资源 ID 中提取资源组和资源名称。"""
    parts = resource_id.strip("/").split("/")
    resource_groups_index = next(index for index, part in enumerate(parts) if part.lower() == "resourcegroups")
    return parts[resource_groups_index + 1], parts[-1]


def enum_value(value):
    return getattr(value, "value", value) if value is not None else None


def vm_power_state(compute_client, resource_group, vm_name):
    instance_view = compute_client.virtual_machines.instance_view(resource_group, vm_name)
    for status in getattr(instance_view, "statuses", []) or []:
        code = getattr(status, "code", "") or ""
        if code.lower().startswith("powerstate/"):
            state = code.split("/", 1)[1].lower()
            return {
                "running": "运行中",
                "starting": "启动中",
                "stopping": "停止中",
                "stopped": "已停止",
                "deallocating": "释放中",
                "deallocated": "已停止并释放",
            }.get(state, getattr(status, "display_status", None) or state)
    return "未知"


def vm_network_addresses(network_client, vm):
    private_ips = []
    public_ips = []
    network_profile = getattr(vm, "network_profile", None)
    for nic_reference in getattr(network_profile, "network_interfaces", []) or []:
        nic_resource_group, nic_name = resource_id_parts(nic_reference.id)
        nic = network_client.network_interfaces.get(nic_resource_group, nic_name)
        for ip_configuration in getattr(nic, "ip_configurations", []) or []:
            private_ip = getattr(ip_configuration, "private_ip_address", None)
            if private_ip:
                private_ips.append(private_ip)
            public_ip_reference = getattr(ip_configuration, "public_ip_address", None)
            if not public_ip_reference or not getattr(public_ip_reference, "id", None):
                continue
            public_ip_resource_group, public_ip_name = resource_id_parts(public_ip_reference.id)
            public_ip = network_client.public_ip_addresses.get(public_ip_resource_group, public_ip_name)
            public_ip_address = getattr(public_ip, "ip_address", None)
            if public_ip_address:
                public_ips.append(public_ip_address)
    return private_ips, public_ips


def list_vms(subscription_id, credential):
    network_client = NetworkManagementClient(credential, subscription_id)
    compute_client = ComputeManagementClient(credential, subscription_id)
    virtual_machines = []
    for vm in compute_client.virtual_machines.list_all():
        resource_group, _ = resource_id_parts(vm.id)
        details_error = None
        try:
            private_ips, public_ips = vm_network_addresses(network_client, vm)
            power_state = vm_power_state(compute_client, resource_group, vm.name)
        except Exception:
            logger.exception("Failed to load details for VM %s/%s", resource_group, vm.name)
            private_ips, public_ips = [], []
            power_state = "读取失败"
            details_error = "部分详情读取失败"

        hardware_profile = getattr(vm, "hardware_profile", None)
        storage_profile = getattr(vm, "storage_profile", None)
        os_disk = getattr(storage_profile, "os_disk", None)
        virtual_machines.append({
            "name": vm.name,
            "resource_group": resource_group,
            "location": getattr(vm, "location", None) or "-",
            "size": enum_value(getattr(hardware_profile, "vm_size", None)) or "-",
            "os_type": enum_value(getattr(os_disk, "os_type", None)) or "-",
            "power_state": power_state,
            "private_ips": private_ips,
            "public_ips": public_ips,
            "details_error": details_error,
        })
    return sorted(virtual_machines, key=lambda item: (item["resource_group"].lower(), item["name"].lower()))
