OUTPUT_ARCH(riscv)
ENTRY(_start)
MEMORY {
    ROM (rx)  : ORIGIN = 0x00010000, LENGTH = 64K
    RAM (rwx) : ORIGIN = 0x10000200, LENGTH = 32K - 512
}
SECTIONS {
    .text : { KEEP(*(.text.start)) *(.text .text.*) } > ROM
    .rodata : { *(.srodata .srodata.* .rodata .rodata.*) . = ALIGN(4); } > ROM
    .data : {
        . = ALIGN(4); __data_start = .;
        PROVIDE(__global_pointer$ = . + 0x800);
        *(.sdata .sdata.* .data .data.*)
        . = ALIGN(4); __data_end = .;
    } > RAM AT > ROM
    __data_load = LOADADDR(.data);
    .bss (NOLOAD) : {
        . = ALIGN(4); __bss_start = .;
        *(.sbss .sbss.* .bss .bss.*) *(COMMON)
        . = ALIGN(4); __bss_end = .;
    } > RAM
    __stack_top = 0x10008000;
    ASSERT(__bss_end + 4096 <= __stack_top, "Firmware leaves less than 4 KiB stack")
    /DISCARD/ : { *(.comment .note.* .eh_frame*) }
}
