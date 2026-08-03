# os-release fixtures

Real `/etc/os-release` files, so distro detection is tested against what
distros actually ship rather than against this developer's machine.

`arch`, `fedora`, `tumbleweed` and `ubuntu` were captured verbatim from the
upstream container images:

```bash
podman run --rm docker.io/library/ubuntu:24.04 cat /etc/os-release
podman run --rm docker.io/library/archlinux:latest cat /etc/os-release
podman run --rm quay.io/fedora/fedora:44 cat /etc/os-release
podman run --rm docker.io/opensuse/tumbleweed:latest cat /etc/os-release
```

`bazzite` and `steamos` are transcribed by hand: neither ships a container
image small enough to be worth pulling for one file, and SteamOS has none.
They are the two immutable cases, and immutability cannot be verified from a
container anyway — a container shares the host kernel, so `/run/ostree-booted`
and a read-only `/usr` reflect the host, not the image.
