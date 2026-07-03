from . import unifi, proxmox, docker, homeassistant

CONNECTORS = {
    "unifi": unifi,
    "proxmox": proxmox,
    "docker": docker,
    "homeassistant": homeassistant,
}
