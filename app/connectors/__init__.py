from . import unifi, proxmox, docker, homeassistant, synology

CONNECTORS = {
    "unifi": unifi,
    "proxmox": proxmox,
    "docker": docker,
    "homeassistant": homeassistant,
    "synology": synology,
}
