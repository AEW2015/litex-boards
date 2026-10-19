    .section .text.start,"ax",@progbits
    .globl _start
_start:
    .option push
    .option norelax
    la gp, __global_pointer$
    .option pop
    la sp, __stack_top
    csrci mstatus, 8
    la t0, trap_entry
    csrw mtvec, t0
    la a0, __data_load
    la a1, __data_start
    la a2, __data_end
1:  bgeu a1, a2, 2f
    lw t0, 0(a0)
    sw t0, 0(a1)
    addi a0, a0, 4
    addi a1, a1, 4
    j 1b
2:  la a0, __bss_start
    la a1, __bss_end
3:  bgeu a0, a1, 4f
    sw zero, 0(a0)
    addi a0, a0, 4
    j 3b
4:  call main
5:  j 5b
    .balign 4
trap_entry:
    csrr a0, mcause
    csrr a1, mepc
    call firmware_trap
6:  j 6b
