# Agilex 5 bank-3B DDR handoff guide

This directory is a documentation handoff for the E065B Modular ES fabric DDR investigation. It is not a new official LiteX-Boards platform or target, and it adds no board implementation.

The selected LiteDRAM source overlay is on branch [`agilex5/dual-ddr-bank3b-timing-20261015`](https://github.com/AEW2015/litedram/tree/agilex5/dual-ddr-bank3b-timing-20261015), under [`contrib/agilex5/`](https://github.com/AEW2015/litedram/tree/agilex5/dual-ddr-bank3b-timing-20261015/contrib/agilex5). The accompanying [bank-3B investigation notes](https://github.com/AEW2015/litedram/blob/agilex5/dual-ddr-bank3b-timing-20261015/contrib/agilex5/BANK3B_HANDOFF.md) describe the source layout, evidence and limits. The QSF/SDC snapshots retain original project paths and are not a standalone LiteX-Boards build.

Current evidence is asymmetric: DDR1600 passes nominal functional tests on both 8-GiB banks; DDR2133.333 passes scoped internal timing review but bank 1 fails the hardware MPR check; DDR2666.667 is unqualified experimental source work and exceeds the selected generated PHY's documented CK range. These outcomes are not a hardware signoff or a guarantee for another carrier/revision.

The source overlay pins LiteDRAM, LiteX and Migen revisions and omits generated RTL and vendor/toolchain snapshots. Its bootstrap and controller replay reproduce all three preserved controller hashes. That is source validation; no new full board build or hardware test was run for this publication.

```sh
git clone --branch agilex5/dual-ddr-bank3b-timing-20261015 https://github.com/AEW2015/litedram.git
cd litedram/contrib/agilex5
python3 bootstrap_deps.py
```

Follow the source README for Python environment setup, regression tests and controller replay commands. The registered-row-hit change is in the fork's `litedram/core/bankmachine.py`; custom PHY/controller sources are in the overlay. The LiteX dependency snapshot has no local source changes, so this handoff creates no unnecessary LiteX patch.

This guide does not transfer ownership of the live FPGA or authorize programming, clock changes, reset/calibration, JTAG, or UART access.
