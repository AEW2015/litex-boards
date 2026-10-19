/* Autonomous CPU APB cold initialization, destructive retuning and HBM tests.
 * The host writes mailbox.command=1 only after checking the board's PL
 * temperature. Firmware does not depend on a host APB initializer.
 */
#include <stdint.h>

typedef struct { uint32_t address, value; } reg_write;
typedef struct { uint32_t mhz, pll_words[3], refresh_period_cycles; } clock_profile;
#include "profile.h"

typedef struct {
    uint32_t magic, version, command, state, failure, step, apb_address, apb_value;
    uint32_t calibrated_mask, writes, reads, errors, temp0, temp1, heartbeat, sequence;
    uint32_t signature, memory_address, expected, actual, byte_writes, ready_status;
    uint32_t bus_errors_before, bus_errors_after, hbm_mhz, sys_hz, byte_reads;
    uint32_t written_channels, checked_channels, selected_bank;
    uint32_t stress_rounds, stress_words, stress_completed;
    uint32_t target_mhz, transition_count, contents_generation;
    uint32_t runtime_status, drained_mask, runtime_error, command_result;
} mailbox;
static volatile mailbox *const mb = (volatile mailbox *)0x10000000u;
static uint32_t phy06[2][8];

enum { WAIT = 1, INITIALIZING = 2, READY = 3, TESTING = 4, STOPPED = 5, QUIESCING = 6 };
enum { BAD_ABI = 1, APB_TIMEOUT = 2, APB_ERROR = 3, DFI_TIMEOUT = 4,
       CAL_TIMEOUT = 5, CAL_WINDOW = 6, DRIVE_CHANGED = 7, BAD_TEMP = 8,
       TRIP = 9, COMMIT_FAILED = 10, MEMORY_ERROR = 11, BUS_ERROR = 12,
       DRAIN_TIMEOUT = 13, DRAIN_ERROR = 14, INTERVAL_MODE = 15 };
static const clock_profile *active_profile;

static inline uint32_t rd(uint32_t address) { return *(volatile uint32_t *)address; }
static inline void wr(uint32_t address, uint32_t value) {
    *(volatile uint32_t *)address = value;
    __asm__ volatile("fence iorw, iorw" ::: "memory");
}
static uint32_t ticks(void) { wr(TIMER_BASE + 12, 1); return rd(TIMER_BASE + 16); }
static void delay_us(uint32_t us) {
    uint32_t start = ticks(), duration = us * 250u;
    while ((uint32_t)(start - ticks()) < duration) { }
}
static void delay_ms(uint32_t ms) { while (ms--) delay_us(1000); }
static void mark(uint32_t phase, uint32_t stack, uint32_t region) {
    mb->step = (phase << 16) | (stack << 8) | region;
    mb->heartbeat++;
}
static void fail(uint32_t reason) __attribute__((noreturn));
static void fail(uint32_t reason) {
    /* A failed drain may still own transactions. Keep admission closed and
     * allow their responses to retire without asserting native AXI reset. */
    if (reason == DRAIN_ERROR || reason == DRAIN_TIMEOUT) wr(HBM_RUNTIME_BASE, 1);
    else wr(HBM_CONTROL_BASE + 16, 16); /* BLOCK is a SYS-domain operation. */
    mb->failure = reason;
    mb->errors++;
    mb->ready_status = rd(HBM_CONTROL_BASE + 20);
    mb->state = 0x80000000u;
    for (;;) { mb->heartbeat++; delay_ms(1); }
}
void firmware_trap(uint32_t cause, uint32_t pc) {
    if (mb->state == QUIESCING || (rd(HBM_RUNTIME_BASE + 12) & 4))
        wr(HBM_RUNTIME_BASE, 1);
    else wr(HBM_CONTROL_BASE + 16, 16);
    mb->failure = 0x100u | cause;
    mb->errors++;
    mb->memory_address = pc;
    mb->ready_status = rd(HBM_CONTROL_BASE + 20);
    mb->state = 0x80000000u;
    for (;;) { mb->heartbeat++; }
}
static void check_trip(void) {
    uint32_t status = rd(HBM_CONTROL_BASE + 20);
    mb->ready_status = status;
    if (status & 0x00030000u) fail(TRIP);
}
static uint32_t apb(uint32_t stack, uint32_t address, uint32_t value, uint32_t write) {
    uint32_t start = ticks(), status;
    mb->apb_address = address;
    mb->apb_value = value;
    do {
        status = rd(HBM_CONTROL_BASE + 20);
        if ((uint32_t)(start - ticks()) >= 1250000u) fail(APB_TIMEOUT);
    } while (status & 1);
    check_trip();
    wr(HBM_CONTROL_BASE + 16, 8); /* Clear retired sticky operation flags. */
    wr(HBM_CONTROL_BASE + 8, address);
    if (write) wr(HBM_CONTROL_BASE + 12, value);
    wr(HBM_CONTROL_BASE + 16, 1 | (write ? 2 : 0) | (stack << 2));
    for (;;) {
        status = rd(HBM_CONTROL_BASE + 20);
        if (status & 8) fail(APB_TIMEOUT);
        if (status & 4) fail(APB_ERROR);
        if ((uint32_t)(start - ticks()) >= 1250000u) fail(APB_TIMEOUT);
        if ((status & 3) == 2) break;
    }
    mb->heartbeat++;
    if (!write) value = rd(HBM_CONTROL_BASE + 24);
    mb->apb_value = value;
    return value;
}
static void table(uint32_t stack, uint32_t offset, const reg_write *entries, uint32_t count) {
    for (uint32_t i = 0; i < count; i++) apb(stack, offset + entries[i].address, entries[i].value, 1);
}
static void poll(uint32_t stack, uint32_t address, uint32_t mask, uint32_t expected, uint32_t error) {
    for (uint32_t attempt = 0; attempt < 128; attempt++) {
        if ((apb(stack, address, 0, 0) & mask) == expected) return;
        if (attempt != 127) delay_ms(1);
    }
    fail(error);
}
static uint32_t temperature(uint32_t stack) {
    apb(stack, 0x240004, 0x00a0800f, 1);
    poll(stack, 0x240004, 0x00ffffff, 0x0020800f, BAD_TEMP);
    uint32_t raw = apb(stack, 0x24000c, 0, 0), temp = (raw >> 24) & 127;
    /* JEDEC HBM2: the temperature byte's high bit is 0=valid. */
    if ((raw & 0x80000000u) || temp == 0 || temp >= 60) fail(BAD_TEMP);
    wr(HBM_CONTROL_BASE + 32 + 4 * stack, 256 | temp);
    if (stack == 0) mb->temp0 = temp; else mb->temp1 = temp;
    return temp;
}
static void refresh_temperatures(void) { check_trip(); temperature(0); temperature(1); }
static const clock_profile *find_profile(uint32_t mhz) {
    for (uint32_t i = 0; i < CLOCK_PROFILE_COUNT; i++)
        if (clock_profiles[i].mhz == mhz) return &clock_profiles[i];
    return 0;
}
static void quiesce(void) {
    mb->state = QUIESCING;
    __asm__ volatile("fence iorw, iorw" ::: "memory");
    wr(HBM_RUNTIME_BASE, 1);
    uint32_t start = ticks();
    for (;;) {
        mb->runtime_error = rd(HBM_RUNTIME_BASE + 8);
        mb->runtime_status = rd(HBM_RUNTIME_BASE + 12);
        mb->drained_mask = rd(HBM_RUNTIME_BASE + 4);
        if (mb->runtime_error || (mb->runtime_status & 4)) fail(DRAIN_ERROR);
        if ((mb->runtime_status & 3) == 3 && mb->drained_mask == 0xffffffffu) return;
        if ((uint32_t)(start - ticks()) >= 25000000u) fail(DRAIN_TIMEOUT);
        check_trip();
    }
}
static void resume(void) {
    if (rd(HBM_RUNTIME_BASE + 8)) fail(DRAIN_ERROR);
    wr(HBM_RUNTIME_BASE, 0);
    mb->runtime_status = rd(HBM_RUNTIME_BASE + 12);
    if (mb->runtime_status != 2) fail(DRAIN_ERROR);
}
static void initialize(const clock_profile *profile) {
    quiesce();
    mb->state = INITIALIZING;
    mb->calibrated_mask = 0;
    wr(HBM_CONTROL_BASE + 16, 16);
    if (rd(HBM_CONTROL_BASE) != 0x4c48424d || rd(HBM_CONTROL_BASE + 4) != 1) fail(BAD_ABI);
    /* These optional maximum-interval modes have no scaled policy here.
     * Reject them before changing any PLL or memory-controller settings. */
    static const uint32_t intervals[] = {0x10422c, 0x104324, 0x104328, 0x10432c};
    for (uint32_t stack = 0; stack < 2; stack++)
        for (uint32_t region = 0; region < 8; region++)
            for (uint32_t i = 0; i < 4; i++)
                if (apb(stack, region * 0x20000 + intervals[i], 0, 0)) fail(INTERVAL_MODE);
    active_profile = profile;
    mb->contents_generation++; /* All prior application buffers are invalid. */
    for (uint32_t stack = 0; stack < 2; stack++)
        for (uint32_t region = 0; region < 8; region++) {
            mark(1, stack, region);
            phy06[stack][region] = apb(stack, region * 0x20000 + 0x18, 0, 0);
        }
    for (uint32_t stack = 0; stack < 2; stack++) {
        mark(2, stack, 0);
        for (uint32_t region = 0; region < 8; region++) apb(stack, region * 0x20000 + 0x104000, 0, 1);
        delay_ms(1000);
        mark(3, stack, 0); table(stack, 0, ms_pre, MS_PRE_COUNT);
        mark(4, stack, 0);
        for (uint32_t i = 0; i < PLL_COUNT; i++)
            apb(stack, pll[i].address, i < 3 ? profile->pll_words[i] : pll[i].value, 1);
        for (uint32_t i = 0; i < 4; i++)
            if (apb(stack, pll[i].address, 0, 0) != (i < 3 ? profile->pll_words[i] : pll[i].value)) fail(COMMIT_FAILED);
        mark(5, stack, 0); table(stack, 0, ms_post, MS_POST_COUNT);
        for (uint32_t region = 0; region < 8; region++) {
            mark(6, stack, region); table(stack, region * 0x20000, phy, PHY_COUNT);
            mark(7, stack, region);
            for (uint32_t i = 0; i < MC_COUNT; i++)
                apb(stack, region * 0x20000 + mc[i].address,
                    mc[i].address == 0x1040cc ? profile->refresh_period_cycles : mc[i].value, 1);
            if (apb(stack, region * 0x20000 + 0x1040cc, 0, 0) != profile->refresh_period_cycles) fail(COMMIT_FAILED);
        }
        for (uint32_t region = 0; region < 8; region++) {
            uint32_t offset = region * 0x20000;
            mark(8, stack, region);
            apb(stack, offset + 0x110050, 1, 1);
            poll(stack, offset + 0x110034, 1, 1, DFI_TIMEOUT);
            apb(stack, offset + 0x104000, 1, 1);
            apb(stack, offset + 0x11000c, 1, 1);
        }
        for (uint32_t region = 0; region < 8; region++) {
            uint32_t offset = region * 0x20000;
            mark(9, stack, region);
            poll(stack, offset + 0x10, CALIBRATION_STATUS_MASK, CALIBRATION_STATUS_EXPECTED, CAL_TIMEOUT);
            for (uint32_t selector = 0; selector < 4; selector++) {
                apb(stack, offset + 0x54, selectors[selector], 1);
                if ((apb(stack, offset + 0x60, 0, 0) & CALIBRATION_RESULT_MASK) != CALIBRATION_RESULT_EXPECTED) fail(CAL_WINDOW);
            }
            mb->calibrated_mask |= 1u << (8 * stack + region);
            if (apb(stack, offset + 0x18, 0, 0) != phy06[stack][region]) fail(DRIVE_CHANGED);
        }
        mark(10, stack, 0); temperature(stack);
    }
    refresh_temperatures();
    mark(11, 0, 0);
    if (mb->calibrated_mask != 0xffff) fail(COMMIT_FAILED);
    wr(HBM_CONTROL_BASE + 16, 32);
    if (((rd(HBM_CONTROL_BASE + 20) >> 8) & 3) != 3) fail(COMMIT_FAILED);
    mb->hbm_mhz = profile->mhz;
    resume();
    mb->state = READY;
}
static uint32_t pattern(uint32_t channel, uint32_t offset, uint32_t word) {
    return 0x94660000u ^ (channel << 20) ^ offset ^ (word * 0x10203u) ^ mb->sequence;
}
static void write_word(uint32_t address, uint32_t value) { wr(address, value); mb->writes++; }
static void read_word(uint32_t address, uint32_t expected) {
    uint32_t actual = rd(address); mb->reads++;
    mb->memory_address = address; mb->expected = expected; mb->actual = actual;
    if (actual != expected) fail(MEMORY_ERROR);
    mb->signature = (mb->signature << 1) ^ actual;
}
static uint32_t channel_base(uint32_t channel) {
    uint32_t bank = channel >> 2;
    __asm__ volatile("fence iorw, iorw" ::: "memory");
    wr(HBM_WINDOW_BASE, bank);
    if (rd(HBM_WINDOW_BASE) != bank) fail(BAD_ABI);
    /* Storage echoes requests even if the guarded effective bank rejected
     * one. Detect that fault before issuing any HBM memory transaction. */
    if (rd(HBM_RUNTIME_BASE + 12) & 4) fail(DRAIN_ERROR);
    mb->selected_bank = bank;
    return 0x40000000u + (channel & 3u) * 0x10000000u;
}
static void transactions(void) {
    static const uint32_t offsets[] = {0, 0x08000000, 0x0fffffe0, 0x00003ff0, 0x00020000};
    mb->state = TESTING; mb->sequence++; mb->writes = 0; mb->reads = 0;
    mb->byte_writes = 0; mb->byte_reads = 0; mb->signature = 0;
    mb->written_channels = 0; mb->checked_channels = 0;
    mb->bus_errors_before = rd(CTRL_BUS_ERRORS);
    refresh_temperatures();
    for (uint32_t channel = 0; channel < 32; channel++) {
        mark(12, channel >> 4, channel);
        refresh_temperatures();
        uint32_t base = channel_base(channel);
        for (uint32_t sample = 0; sample < 5; sample++) {
            uint32_t words = sample < 3 ? 8 : (sample == 3 ? 32 : 64);
            for (uint32_t i = 0; i < words; i++) write_word(base + offsets[sample] + 4 * i, pattern(channel, offsets[sample], i));
        }
        mb->written_channels |= 1u << channel;
    }
    /* All channel/address markers were written before reading any of them. */
    for (uint32_t channel = 0; channel < 32; channel++) {
        uint32_t base = channel_base(channel);
        refresh_temperatures();
        for (uint32_t sample = 0; sample < 5; sample++) {
            uint32_t words = sample < 3 ? 8 : (sample == 3 ? 32 : 64);
            for (uint32_t i = 0; i < words; i++) read_word(base + offsets[sample] + 4 * i, pattern(channel, offsets[sample], i));
        }
        uint32_t expected = 0xa5c35a7e;
        write_word(base + 0x3000, expected);
        for (uint32_t lane = 0; lane < 4; lane++) {
            uint32_t value = 0x10 + lane;
            *(volatile uint8_t *)(base + 0x3000 + lane) = (uint8_t)value;
            __asm__ volatile("fence iorw, iorw" ::: "memory");
            mb->byte_writes++;
            expected = (expected & ~(255u << (8 * lane))) | (value << (8 * lane));
            read_word(base + 0x3000, expected);
            uint32_t actual = *(volatile uint8_t *)(base + 0x3000 + lane); mb->byte_reads++;
            if (actual != value) { mb->actual = actual; mb->expected = value; fail(MEMORY_ERROR); }
        }
        mb->checked_channels |= 1u << channel;
    }
    channel_base(0);
    mb->bus_errors_after = rd(CTRL_BUS_ERRORS);
    if (mb->bus_errors_after != mb->bus_errors_before) fail(BUS_ERROR);
    refresh_temperatures();
    mb->state = READY;
}
/* Bounded sustained traffic. The host may repeat CMD4 while monitoring PL.
 * Each round writes all channels before checking any channel, so aliases
 * cannot pass by reading data immediately after writing the same location.
 */
static uint32_t stress_pattern(uint32_t channel, uint32_t offset, uint32_t word, uint32_t round) {
    uint32_t value = pattern(channel, offset, word);
    switch (round & 3u) {
    case 1: return 1u << (word & 31u);
    case 2: return ~(1u << (word & 31u));
    case 3:
        value ^= value << 13; value ^= value >> 17; value ^= value << 5;
        return value;
    default: return value;
    }
}
static uint32_t stress_stop(void) {
    if (mb->command != 3) return 0;
    mb->command = 0;
    quiesce();
    channel_base(0);
    wr(HBM_CONTROL_BASE + 16, 16);
    mb->ready_status = rd(HBM_CONTROL_BASE + 20);
    mb->state = STOPPED;
    return 1;
}
static void stress(void) {
    uint32_t rounds = mb->stress_rounds, words = mb->stress_words;
    if (rounds == 0 || rounds > 32 || words == 0 || words > 4096) fail(BAD_ABI);
    mb->state = TESTING; mb->sequence++; mb->stress_completed = 0;
    mb->writes = 0; mb->reads = 0; mb->byte_writes = 0; mb->byte_reads = 0;
    mb->signature = 0; mb->written_channels = 0; mb->checked_channels = 0;
    mb->bus_errors_before = rd(CTRL_BUS_ERRORS);
    for (uint32_t round = 0; round < rounds; round++) {
        uint32_t offset = 0x00100000u + round * 0x4000u;
        for (uint32_t channel = 0; channel < 32; channel++) {
            uint32_t base = channel_base(channel);
            mark(13, channel >> 4, channel);
            for (uint32_t word = 0; word < words; word++) {
                if ((word & 255u) == 0) {
                    if (stress_stop()) return;
                    refresh_temperatures();
                }
                write_word(base + offset + 4*word, stress_pattern(channel, offset, word, round));
            }
            mb->written_channels |= 1u << channel;
        }
        for (uint32_t channel = 0; channel < 32; channel++) {
            uint32_t base = channel_base(channel);
            for (uint32_t word = 0; word < words; word++) {
                if ((word & 255u) == 0) {
                    if (stress_stop()) return;
                    refresh_temperatures();
                }
                read_word(base + offset + 4*word, stress_pattern(channel, offset, word, round));
            }
            mb->checked_channels |= 1u << channel;
        }
        mb->stress_completed++;
    }
    channel_base(0);
    mb->bus_errors_after = rd(CTRL_BUS_ERRORS);
    if (mb->bus_errors_after != mb->bus_errors_before) fail(BUS_ERROR);
    refresh_temperatures();
    mb->state = READY;
}
int main(void) {
    wr(TIMER_BASE + 8, 0); wr(TIMER_BASE, 0xffffffffu);
    wr(TIMER_BASE + 4, 0xffffffffu); wr(TIMER_BASE + 28, 0); wr(TIMER_BASE + 8, 1);
    for (uint32_t i = 0; i < 128; i++) ((volatile uint32_t *)mb)[i] = 0;
    mb->magic = 0x464b4842; mb->version = 3; mb->hbm_mhz = BOOT_HBM_MHZ; mb->sys_hz = 250000000;
    active_profile = find_profile(BOOT_HBM_MHZ);
    if (!active_profile) fail(BAD_ABI);
    mb->target_mhz = BOOT_HBM_MHZ;
    mb->temp0 = 0xffffffffu; mb->temp1 = 0xffffffffu; mb->state = WAIT;
    for (;;) {
        uint32_t command = mb->command;
        if (command) mb->command = 0;
        if (command == 1 && (mb->state == WAIT || mb->state == STOPPED)) { initialize(active_profile); transactions(); }
        if (command == 2 && mb->state == READY) transactions();
        if (command == 4 && mb->state == READY) stress();
        if (command == 5 && (mb->state == READY || mb->state == STOPPED)) {
            const clock_profile *profile = find_profile(mb->target_mhz);
            mb->command_result = profile ? 0 : 1;
            if (profile) {
                initialize(profile);
                mb->transition_count++;
                transactions();
            }
        }
        if (command == 3) {
            quiesce();
            channel_base(0);
            wr(HBM_CONTROL_BASE + 16, 16);
            mb->ready_status = rd(HBM_CONTROL_BASE + 20);
            mb->state = STOPPED;
        }
        if (mb->state == READY) refresh_temperatures();
        mb->heartbeat++;
        delay_ms(10);
    }
}
