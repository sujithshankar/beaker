TFTP files and directories
==========================

As part of the :ref:`provisioning process <provisioning-process>`, test systems 
fetch boot loader images and configuration files over TFTP from the Beaker lab 
controller.
This section describes all the files under the TFTP root directory that the 
:program:`beaker-provision` daemon either creates, or relies on indirectly, 
during the provisioning process.

.. _boot-loader-images:

Boot loader images
------------------

These images must be supplied by the Beaker administrator and copied into the 
TFTP root directory manually (with the exception of :file:`pxelinux.0`).
The Cobbler project provides `pre-compiled binaries of common boot loaders 
<https://github.com/cobbler/cobbler.github.com/tree/master/loaders>`__. Many 
Linux distributions also package these boot loaders.

Each test system should have a suitable DHCP option pointing at one of these 
boot loader images (see :ref:`adding-systems`).

:file:`pxelinux.0`
    Recommended location of the PXELINUX image, used for x86-based systems with 
    BIOS firmware. PXELINUX is a network boot loader developed as part of the 
    Syslinux_ project.

    If this file does not exist, Beaker copies the PXELINUX image from the  
    Syslinux package to this location so that x86 BIOS systems can be 
    provisioned out of the box.

    It is possible to use alternative names for this image (for example, to 
    have multiple parallel versions of PXELINUX for compatibility reasons), 
    assuming that the DHCP configuration is adjusted configured accordingly.

:file:`grub/grub.efi`
    Recommended location of the EFI GRUB_ image, used for x86-based systems 
    with UEFI firmware.

    It is possible to use alternative names for this image, but the image must 
    be under the :file:`grub/` directory.

:file:`yaboot`
    Location of the Yaboot_ image, used for PowerPC systems.

:file:`elilo-ia64.efi`
    Location of the ELILO_ image, used for IA64 systems.

:file:`aarch64/bootaa64.efi`
    Location of the GRUB2 boot loader image for 64-bit ARM systems.

:file:`boot/grub2/powerpc-ieee1275`
    Location of the GRUB2 boot loader and supporting files for PowerPC
    (PPC64) systems.

.. _Syslinux: http://www.syslinux.org/
.. _GRUB: http://www.gnu.org/software/grub/
.. _ELILO: http://elilo.sourceforge.net/
.. _Yaboot: http://yaboot.ozlabs.org/

Boot loader configuration files
-------------------------------

Beaker creates these files when a test system is being provisioned, and then 
removes them once the installation is complete.

:file:`pxelinux.cfg/{0A010203}`
    Configuration for PXELINUX. The filename is the IPv4 address of the test 
    system, represented as 8 hexadecimal digits (using uppercase letters).

:file:`grub/images`
    Symlink to the :file:`images` directory.

:file:`grub/{0A010203}`
    Configuration for EFI GRUB. The filename follows the PXELINUX naming 
    convention.

:file:`ppc/{0a010203}`
    Symbolic link to the Yaboot image. The filename is the IPv4 address of the 
    test system, represented as 8 hexadecimal digits (using lowercase letters).

:file:`etc/{0a010203}`
    Configuration for Yaboot. The filename matches the boot loader symlink 
    filename.

:file:`ppc/{0a010203}-grub2`
    Symbolic link to the GRUB2 boot loader. The filename is prefixed
    with the IPv4 address of the test system, represented as 8
    hexadecimal digits (using lowercase letters).

:file:`ppc/grub.cfg-{0A1043DE}`; :file:`boot/grub2/grub.cfg-{0A1043DE}`; :file:`grub.cfg-{0A1043DE}`
    Configuration for GRUB2 for PowerPC (PPC64) systems. The filename
    is suffixed with the IPv4 address of the test system, represented
    as 8 hexadecimal digits (using uppercase letters).

:file:`{0A010203}.conf`
    Configuration for ELILO. The filename follows the PXELINUX naming 
    convention.

:file:`arm/empty`
    An empty file.

:file:`arm/pxelinux.cfg/{0A010203}`
    Configuration for 32-bit ARM systems. The filename follows the PXELINUX 
    naming convention.

:file:`aarch64/grub.cfg-{0A010203}`
    Configuration for 64-bit ARM systems.

:file:`s390x/s_{fqdn}`; :file:`s390x/s_{fqdn}_parm`; :file:`s390x/s_{fqdn}_conf`
    Configuration files for System/390 virtual machines using "zPXE" (Cobbler's 
    ``zpxe.rexx`` script).

:file:`images/{fqdn}/`
    Kernel and initrd images for the distro being provisioned. All the 
    generated boot loader configurations point at the images in this directory.


Other files in the TFTP root directory
--------------------------------------

:file:`pxelinux.cfg/default`
    Default configuration used by PXELINUX when no system-specific 
    configuration exists.
    
    The Beaker administrator can customize this configuration, however it must 
    fall back to booting the local disk by default (perhaps after a timeout) 
    using the ``localboot 0`` command.

    If this file does not exist, Beaker populates it with a simple default 
    configuration that immediately boots the local disk.

:file:`aarch64/grub.cfg`
    Default configuration used by 64-bit ARM systems when no system-specific 
    configuration exists.

    The Beaker administrator can customize this configuration, however it 
    should exit after a timeout using the ``exit`` command.

    If this file does not exist, Beaker populates it with a simple default 
    configuration that immediately exits.

:file:`ppc/grub.cfg`
    Default configuration used by PowerPC systems when no system-specific 
    configuration exists.

    The Beaker administrator can customize this configuration, however it 
    should exit after a timeout using the ``exit`` command.

    If this file does not exist, Beaker populates it with a simple default 
    configuration that immediately exits.


:file:`pxelinux.cfg/beaker_menu`
    Menu configuration generated by :program:`beaker-pxemenu` for the 
    ``menu.c32`` program (part of Syslinux). See :ref:`pxe-menu` for details.

:file:`grub/efidefault`
    Menu configuration generated by :program:`beaker-pxemenu` for EFI GRUB.

:file:`aarch64/beaker_menu.cfg`
    Menu configuration generated by :program:`beaker-pxemenu` for 64-bit ARM 
    systems.

:file:`distrotrees/`
    Cached images for the generated menus. The contents of this directory are  
    managed by :program:`beaker-pxemenu`.
