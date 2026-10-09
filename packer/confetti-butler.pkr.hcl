# Builds the confetti-butler golden template in vCenter in one command:
# boots the Alpine ISO, installs Alpine to disk with an answer file, uploads
# butler/, runs build-template.sh, shuts down and converts the VM to a template.
# It automates the manual steps in the header of butler/build-template.sh.
#
#   packer init packer/
#   packer build -var-file=packer/vars.pkrvars.hcl packer/
#
# STATUS: NOT RUN. Written from the vsphere-iso plugin docs and Alpine's
# setup-alpine behaviour, but no vCenter was available to try it on. Expect to
# adjust boot_command timings and device names (eth0, /dev/sda) on first use.
# See packer/README.md.

packer {
  required_plugins {
    vsphere = {
      version = ">= 1.2.0"
      source  = "github.com/hashicorp/vsphere"
    }
  }
}

variable "vcenter_server" { type = string }
variable "vcenter_username" { type = string }
variable "vcenter_password" {
  type      = string
  sensitive = true
}
variable "insecure_connection" {
  type    = bool
  default = false
}
variable "datacenter" { type = string }
variable "cluster" { type = string }
variable "datastore" { type = string }
variable "network" { type = string }
variable "folder" {
  type    = string
  default = ""
}
variable "iso_path" {
  type        = string
  description = "Alpine 'virt' ISO already on a datastore, e.g. [datastore1] iso/alpine-virt-3.24.2-x86_64.iso"
}
variable "root_password" {
  type        = string
  sensitive   = true
  description = "Root password of the template. Also used by Packer to SSH in during the build."
}
variable "vm_name" {
  type    = string
  default = "confetti-butler-template"
}

source "vsphere-iso" "butler" {
  vcenter_server      = var.vcenter_server
  username            = var.vcenter_username
  password            = var.vcenter_password
  insecure_connection = var.insecure_connection

  datacenter = var.datacenter
  cluster    = var.cluster
  datastore  = var.datastore
  folder     = var.folder

  vm_name       = var.vm_name
  guest_os_type = "other5xLinux64Guest"
  CPUs          = 1
  RAM           = 512

  disk_controller_type = ["pvscsi"]
  storage {
    disk_size             = 4096
    disk_thin_provisioned = true
  }
  network_adapters {
    network      = var.network
    network_card = "vmxnet3"
  }

  iso_paths      = [var.iso_path]
  http_directory = "${path.root}/http"

  # Alpine's live ISO logs root in with no password. Bring up the network,
  # fetch the answer file from Packer's HTTP server, patch in the root
  # password, then run the installer. setup-alpine still asks for the root
  # password (twice) whatever the answer file says, and for confirmation
  # before erasing the disk.
  boot_wait = "25s"
  boot_command = [
    "root<enter><wait3>",
    "ifconfig eth0 up && udhcpc -i eth0 -n -q<enter><wait5>",
    "wget -q -O /tmp/answers http://{{ .HTTPIP }}:{{ .HTTPPort }}/answers<enter><wait3>",
    "setup-alpine -f /tmp/answers<enter><wait15>",
    "${var.root_password}<enter><wait>",
    "${var.root_password}<enter><wait60>",
    "y<enter><wait90>",
    "reboot<enter>"
  ]

  ssh_username = "root"
  ssh_password = var.root_password
  ssh_timeout  = "30m"

  # build-template.sh deletes the SSH host keys and zero-fills free space as
  # its last steps; the connection that started it survives, so the script's
  # exit status still reaches Packer.
  shutdown_command = "poweroff"
  shutdown_timeout = "10m"

  convert_to_template = true
}

build {
  sources = ["source.vsphere-iso.butler"]

  provisioner "file" {
    source      = "${path.root}/../butler"
    destination = "/root/"
  }

  provisioner "shell" {
    environment_vars = ["LAB_ROOT_PASSWORD=${var.root_password}"]
    inline           = ["sh /root/butler/build-template.sh"]
  }
}
