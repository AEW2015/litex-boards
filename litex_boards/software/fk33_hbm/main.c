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
    /* Append-only CMD7 configuration/results; mailbox remains within 128 words. */
    uint32_t traffic_mode, traffic_cycles_limit, traffic_seed;
    uint32_t traffic_status, traffic_errors, traffic_cycles;
    uint32_t traffic_write_beats, traffic_read_beats, traffic_write_bursts, traffic_read_bursts;
    uint32_t traffic_aw_count, traffic_ar_count, traffic_packed_w, traffic_packed_r;
    uint32_t traffic_aw_stalls, traffic_w_stalls, traffic_ar_stalls, traffic_read_wait;
    uint32_t traffic_write_latency_min, traffic_write_latency_max;
    uint32_t traffic_read_latency_min, traffic_read_latency_max;
    uint32_t traffic_random, traffic_write_enabled_bytes_lo, traffic_write_enabled_bytes_hi;
    uint32_t traffic_aw_native_beats, traffic_ar_native_beats;
    uint32_t traffic_aw_packed_words, traffic_ar_packed_words;
    uint32_t traffic_aw_length_seen, traffic_ar_length_seen;
    uint32_t traffic2_mode, traffic2_cycles_limit, traffic2_seed, traffic2_random;
    uint32_t traffic2_status;
    uint32_t traffic2_errors;
    uint32_t traffic2_cycles;
    uint32_t traffic2_write_beats;
    uint32_t traffic2_read_beats;
    uint32_t traffic2_write_bursts;
    uint32_t traffic2_read_bursts;
    uint32_t traffic2_aw_count;
    uint32_t traffic2_ar_count;
    uint32_t traffic2_packed_w;
    uint32_t traffic2_packed_r;
    uint32_t traffic2_aw_stalls;
    uint32_t traffic2_w_stalls;
    uint32_t traffic2_ar_stalls;
    uint32_t traffic2_read_wait;
    uint32_t traffic2_write_latency_min;
    uint32_t traffic2_write_latency_max;
    uint32_t traffic2_read_latency_min;
    uint32_t traffic2_read_latency_max;
    uint32_t traffic2_write_enabled_bytes_lo;
    uint32_t traffic2_write_enabled_bytes_hi;
    uint32_t traffic2_aw_native_beats;
    uint32_t traffic2_ar_native_beats;
    uint32_t traffic2_aw_packed_words;
    uint32_t traffic2_ar_packed_words;
    uint32_t traffic2_aw_length_seen;
    uint32_t traffic2_ar_length_seen;
    uint32_t traffic_pair_elapsed_cycles;
} mailbox;
static volatile mailbox *const mb = (volatile mailbox *)0x10000000u;
static uint32_t phy06[2][8];

enum { WAIT = 1, INITIALIZING = 2, READY = 3, TESTING = 4, STOPPED = 5, QUIESCING = 6 };
enum { BAD_ABI = 1, APB_TIMEOUT = 2, APB_ERROR = 3, DFI_TIMEOUT = 4,
       CAL_TIMEOUT = 5, CAL_WINDOW = 6, DRIVE_CHANGED = 7, BAD_TEMP = 8,
       TRIP = 9, COMMIT_FAILED = 10, MEMORY_ERROR = 11, BUS_ERROR = 12,
       DRAIN_TIMEOUT = 13, DRAIN_ERROR = 14, INTERVAL_MODE = 15,
       ENDPOINT_ERROR = 16, ENDPOINT_TIMEOUT = 17 };
static const clock_profile *active_profile;

static inline uint32_t rd(uint32_t address) { return *(volatile uint32_t *)address; }
static inline void wr(uint32_t address, uint32_t value) {
    *(volatile uint32_t *)address = value;
    __asm__ volatile("fence iorw, iorw" ::: "memory");
}
static uint32_t ticks(void) { wr(TIMER_BASE + 12, 1); return rd(TIMER_BASE + 16); }
static void delay_us(uint32_t us) {
    uint32_t start = ticks(), duration = us * (SYS_CLK_FREQ / 1000000u);
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
    if (reason == DRAIN_ERROR || reason == DRAIN_TIMEOUT ||
            reason == ENDPOINT_ERROR || reason == ENDPOINT_TIMEOUT) wr(HBM_RUNTIME_BASE, 1);
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
        if ((uint32_t)(start - ticks()) >= SYS_CLK_FREQ / 200u) fail(APB_TIMEOUT);
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
        if ((uint32_t)(start - ticks()) >= SYS_CLK_FREQ / 200u) fail(APB_TIMEOUT);
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
        if ((uint32_t)(start - ticks()) >= SYS_CLK_FREQ / 10u) fail(DRAIN_TIMEOUT);
        check_trip();
    }
}
static void resume(void) {
    if (rd(HBM_RUNTIME_BASE + 8)) fail(DRAIN_ERROR);
    wr(HBM_RUNTIME_BASE, 0);
#ifdef HBM_SEPARATE_AXI_DOMAIN
    /* Release completion is acknowledged across SYS/AXI; an immediate CSR
     * read can still observe the old generation. Retain bounded failure and
     * trip handling while waiting for the current idle snapshot. */
    uint32_t start = ticks();
    for (;;) {
        mb->runtime_error = rd(HBM_RUNTIME_BASE + 8);
        mb->runtime_status = rd(HBM_RUNTIME_BASE + 12);
        mb->drained_mask = rd(HBM_RUNTIME_BASE + 4);
        if (mb->runtime_error || (mb->runtime_status & 4)) fail(DRAIN_ERROR);
        if (mb->runtime_status == 2 && mb->drained_mask == 0xffffffffu) return;
        if ((uint32_t)(start - ticks()) >= SYS_CLK_FREQ / 10u) fail(DRAIN_TIMEOUT);
        check_trip();
    }
#else
    mb->runtime_status = rd(HBM_RUNTIME_BASE + 12);
    if (mb->runtime_status != 2) fail(DRAIN_ERROR);
#endif
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
#ifdef HBM_ENDPOINT_PORT
        if (channel == HBM_ENDPOINT_PORT) continue;
#endif
#ifdef HBM_ENDPOINT_SECOND_PORT
        if (channel == HBM_ENDPOINT_SECOND_PORT) continue;
#endif
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
#ifdef HBM_ENDPOINT_PORT
        if (channel == HBM_ENDPOINT_PORT) continue;
#endif
#ifdef HBM_ENDPOINT_SECOND_PORT
        if (channel == HBM_ENDPOINT_SECOND_PORT) continue;
#endif
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
#ifdef HBM_ENDPOINT_PORT
            if (channel == HBM_ENDPOINT_PORT) continue;
#endif
#ifdef HBM_ENDPOINT_SECOND_PORT
            if (channel == HBM_ENDPOINT_SECOND_PORT) continue;
#endif
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
#ifdef HBM_ENDPOINT_PORT
            if (channel == HBM_ENDPOINT_PORT) continue;
#endif
#ifdef HBM_ENDPOINT_SECOND_PORT
            if (channel == HBM_ENDPOINT_SECOND_PORT) continue;
#endif
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
#if defined(HBM_ENDPOINT_PORT) && !defined(HBM_ENDPOINT_TRAFFIC)
/* CMD6 uses only the dedicated endpoint. All expected bytes are calculated
 * here; HBM contents are established by full-strobe writes before masking. */
static uint32_t endpoint_status(void) {
    uint32_t status = rd(EP_STATUS);
    mb->actual = rd(EP_ERRORS);
    if (mb->actual || (status & (1u << 10))) fail(ENDPOINT_ERROR);
    check_trip();
    return status;
}
static void endpoint_wait(uint32_t mask, uint32_t value) {
    uint32_t start = ticks();
    while ((endpoint_status() & mask) != value) {
        if ((uint32_t)(start - ticks()) >= SYS_CLK_FREQ / 10u) fail(ENDPOINT_TIMEOUT);
    }
}
static void endpoint_descriptor(uint32_t read, uint32_t command, uint32_t beats) {
    /* HBM native address includes its pseudochannel tag in bits32:28. */
    uint32_t offset = 0x00100000u + command * 0x200u;
    endpoint_wait(1u << (read ? 2 : 1), 1u << (read ? 2 : 1));
    wr(read ? EP_AR_ADDR_LO : EP_AW_ADDR_LO, (HBM_ENDPOINT_PORT << 28) | offset);
    wr(read ? EP_AR_ADDR_HI : EP_AW_ADDR_HI, HBM_ENDPOINT_PORT >> 4);
    wr(read ? EP_AR_LEN : EP_AW_LEN, beats - 1);
    wr(read ? EP_AR_ID : EP_AW_ID, read ? 0x2au : 0x15u);
    wr(read ? EP_AR_SUBMIT : EP_AW_SUBMIT, 1);
    endpoint_status();
}
static uint32_t endpoint_pattern(uint32_t seed, uint32_t command, uint32_t word) {
    return seed ^ (0x00100000u + command * 0x200u + word * 4u) ^ (word * 0x01020304u);
}
static uint32_t endpoint_strobe(uint32_t command, uint32_t upper, uint32_t masked) {
    if (!masked) return 0xffffffffu;
    switch (command) {
    case 0: return 0;
    case 1: return upper ? 0 : 0xffffffffu;
    case 2: return upper ? 0xffffffffu : 0;
    case 3: return upper ? 0xa5a5a5a5u : 0x5a5a5a5au;
    default: return 0xffffffffu;
    }
}
static void endpoint_write_batch(uint32_t beats, uint32_t count, uint32_t seed, uint32_t masked) {
    for (uint32_t command = 0; command < count; command++) endpoint_descriptor(0, command, beats);
    uint32_t stop = masked && (beats == 3 || beats == 16);
    if (stop) {
        /* All descriptors are accepted by the adapter, but their W words
         * are still owed. At LEN16 the fifth descriptor is also queued. */
        wr(HBM_RUNTIME_BASE, 1);
        delay_us(100);
        if ((rd(HBM_RUNTIME_BASE + 12) & 2) || !(endpoint_status() & 512)) fail(ENDPOINT_ERROR);
    }
    for (uint32_t command = 0; command < count; command++) {
        for (uint32_t word = 0; word < (beats + 1) / 2; word++) {
            endpoint_wait(8, 8);
            for (uint32_t lane = 0; lane < 16; lane++)
                wr(ep_w_data_regs[lane], endpoint_pattern(seed, command, word * 16 + lane));
            wr(EP_W_STRB_LO, endpoint_strobe(command, 0, masked));
            wr(EP_W_STRB_HI, endpoint_strobe(command, 1, masked));
            wr(EP_W_LAST, word + 1 == (beats + 1) / 2);
            wr(EP_W_SUBMIT, 1);
            endpoint_status();
        }
    }
    endpoint_wait(16, 16);
    /* Keep B unconsumed: four credits must remain owned, and an optional
     * fifth descriptor must remain queued until the first B is popped. */
    delay_us(100);
    if (rd(EP_WRITE_CREDITS) != 4 || (count == 5 && rd(EP_AW_LEVEL) != 1)) fail(ENDPOINT_ERROR);
    uint32_t held_id = rd(EP_B_ID), held_resp = rd(EP_B_RESP);
    delay_us(100);
    if (rd(EP_B_ID) != held_id || rd(EP_B_RESP) != held_resp) fail(ENDPOINT_ERROR);
    for (uint32_t command = 0; command < count; command++) {
        endpoint_wait(16, 16);
        if (rd(EP_B_ID) != 0x15 || rd(EP_B_RESP)) fail(ENDPOINT_ERROR);
        wr(EP_B_POP, 1);
        mb->writes += beats;
    }
    endpoint_wait(128, 128);
    if (stop) { quiesce(); resume(); mb->state = TESTING; }
}
static void endpoint_read_batch(uint32_t beats, uint32_t count, uint32_t masked, uint32_t stop) {
    for (uint32_t command = 0; command < count; command++) endpoint_descriptor(1, command, beats);
    endpoint_wait(32, 32);
    delay_us(100);
    if (rd(EP_READ_CREDITS) != 4 || (count == 5 && rd(EP_AR_LEVEL) != 1)) fail(ENDPOINT_ERROR);
    uint32_t held_data = rd(ep_r_data_regs[0]), held_last = rd(EP_R_LAST);
    delay_us(100);
    if (rd(ep_r_data_regs[0]) != held_data || rd(EP_R_LAST) != held_last) fail(ENDPOINT_ERROR);
    if (stop) {
        wr(HBM_RUNTIME_BASE, 1);
        delay_us(100);
        if ((rd(HBM_RUNTIME_BASE + 12) & 2) || !(endpoint_status() & 512)) fail(ENDPOINT_ERROR);
    }
    for (uint32_t command = 0; command < count; command++) {
        for (uint32_t word = 0; word < (beats + 1) / 2; word++) {
            endpoint_wait(32, 32);
            uint32_t upper = word * 2 + 1 < beats;
            uint32_t last = word + 1 == (beats + 1) / 2;
            uint32_t ids = rd(EP_R_ID), responses = rd(EP_R_RESP);
            if (rd(EP_R_KEEP_LO) != 0xffffffffu || rd(EP_R_KEEP_HI) != (upper ? 0xffffffffu : 0) ||
                    rd(EP_R_FIRST) != (word == 0) || rd(EP_R_LAST) != last ||
                    (ids & 63) != 0x2a || (responses & 3) ||
                    (upper && (((ids >> 6) & 63) != 0x2a || (responses >> 2)))) fail(ENDPOINT_ERROR);
            for (uint32_t lane = 0; lane < (upper ? 16u : 8u); lane++) {
                uint32_t expected = endpoint_pattern(0x94660000u, command, word * 16 + lane);
                uint32_t updated = endpoint_pattern(0x5a39c6e1u, command, word * 16 + lane);
                uint32_t strobe = endpoint_strobe(command, lane >= 8, masked);
                uint32_t bytes = (strobe >> (4 * (lane & 7))) & 15;
                uint32_t mask = 0;
                for (uint32_t byte = 0; byte < 4; byte++)
                    if (bytes & (1u << byte)) mask |= 255u << (byte * 8);
                if (masked) expected = (expected & ~mask) | (updated & mask);
                mb->expected = expected; mb->actual = rd(ep_r_data_regs[lane]);
                mb->memory_address = 0x00100000u + command * 0x200u + word * 64 + lane * 4;
                if (mb->actual != expected) fail(ENDPOINT_ERROR);
                mb->signature = (mb->signature << 1) ^ mb->actual;
            }
            wr(EP_R_POP, 1);
        }
        mb->reads += beats;
    }
    endpoint_wait(128, 128);
    if (stop) { quiesce(); resume(); mb->state = TESTING; }
}
static void endpoint_transactions(void) {
    mb->state = TESTING; mb->sequence++; mb->writes = 0; mb->reads = 0;
    mb->signature = 0; mb->command_result = 0;
    mb->bus_errors_before = rd(CTRL_BUS_ERRORS);
    for (uint32_t beats = 1; beats <= 16; beats++) {
        uint32_t count = beats == 16 ? 5 : 4;
        mark(14, HBM_ENDPOINT_PORT, beats);
        refresh_temperatures();
        endpoint_write_batch(beats, count, 0x94660000u, 0);
        endpoint_read_batch(beats, count, 0, 0);
        endpoint_write_batch(beats, count, 0x5a39c6e1u, 1);
        endpoint_read_batch(beats, count, 1, beats == 3 || beats == 16);
    }
    mb->bus_errors_after = rd(CTRL_BUS_ERRORS);
    if (mb->bus_errors_before != mb->bus_errors_after) fail(BUS_ERROR);
    endpoint_status();
    refresh_temperatures();
    mb->state = READY;
}
#endif

#ifdef HBM_ENDPOINT_TRAFFIC
/* CMD7 configures a bounded RTL run. The CPU never transfers traffic payloads. */
static void traffic_statistics(void) {
    mb->traffic_status = rd(TF_STATUS);
    mb->traffic_errors = rd(TF_ERRORS);
    mb->traffic_cycles = rd(TF_CYCLES);
    mb->traffic_write_beats = rd(TF_WRITE_BEATS);
    mb->traffic_read_beats = rd(TF_READ_BEATS);
    mb->traffic_write_bursts = rd(TF_WRITE_BURSTS);
    mb->traffic_read_bursts = rd(TF_READ_BURSTS);
    mb->traffic_aw_count = rd(TF_AW_COUNT);
    mb->traffic_ar_count = rd(TF_AR_COUNT);
    mb->traffic_packed_w = rd(TF_PACKED_W);
    mb->traffic_packed_r = rd(TF_PACKED_R);
    mb->traffic_aw_stalls = rd(TF_AW_STALLS);
    mb->traffic_w_stalls = rd(TF_W_STALLS);
    mb->traffic_ar_stalls = rd(TF_AR_STALLS);
    mb->traffic_read_wait = rd(TF_READ_WAIT);
    mb->traffic_write_latency_min = rd(TF_WRITE_LATENCY_MIN);
    mb->traffic_write_latency_max = rd(TF_WRITE_LATENCY_MAX);
    mb->traffic_read_latency_min = rd(TF_READ_LATENCY_MIN);
    mb->traffic_read_latency_max = rd(TF_READ_LATENCY_MAX);
#ifdef HBM_ENDPOINT_RANDOM
    /* DONE freezes the split 64-bit enabled-byte count before this copy. */
    mb->traffic_write_enabled_bytes_lo = rd(TF_WRITE_ENABLED_BYTES_LO);
    mb->traffic_write_enabled_bytes_hi = rd(TF_WRITE_ENABLED_BYTES_HI);
    mb->traffic_aw_native_beats = rd(TF_AW_NATIVE_BEATS);
    mb->traffic_ar_native_beats = rd(TF_AR_NATIVE_BEATS);
    mb->traffic_aw_packed_words = rd(TF_AW_PACKED_WORDS);
    mb->traffic_ar_packed_words = rd(TF_AR_PACKED_WORDS);
    mb->traffic_aw_length_seen = rd(TF_AW_LENGTH_SEEN);
    mb->traffic_ar_length_seen = rd(TF_AR_LENGTH_SEEN);
#endif
}
static void traffic_run(void) {
    uint32_t mode = mb->traffic_mode;
    uint32_t cycles = mb->traffic_cycles_limit;
    uint32_t seed = mb->traffic_seed;
    mb->command_result = 1;
#ifdef HBM_ENDPOINT_RANDOM
    uint32_t random = mb->traffic_random;
    if (mode > 5 || random > 1 || cycles > (1u << 28) ||
            (mode >= 1 && mode <= 3 && !cycles) ||
            ((mode == 0 || mode >= 4) && cycles) ||
            (mode >= 4 && !random)) return;
#else
    if (mode > 3 || cycles > (1u << 28) || (mode && !cycles)) return;
#endif
    uint32_t status = rd(TF_STATUS);
    if (rd(TF_ERRORS) || (status & 1)) fail(ENDPOINT_ERROR);
    if ((status & ((1u << 3) | (1u << 7))) != ((1u << 3) | (1u << 7)) ||
            (status & (1u << 4))) return;
    refresh_temperatures();
    /* Clear old completion before publishing the next command generation. */
    mb->traffic_status = 0;
    mb->sequence++;
    mb->state = TESTING;
    mb->bus_errors_before = rd(CTRL_BUS_ERRORS);
    wr(TF_CONFIG_MODE, mode);
    wr(TF_CONFIG_CYCLES, cycles);
    wr(TF_CONFIG_SEED, seed);
#ifdef HBM_ENDPOINT_RANDOM
    wr(TF_CONFIG_RANDOM, random);
#endif
    wr(TF_START, 1);
    uint32_t start = ticks(), temperature_tick = start;
    for (;;) {
        uint32_t status = rd(TF_STATUS), errors = rd(TF_ERRORS);
        check_trip();
        if (errors || (status & (1u << 8))) {
            traffic_statistics();
            fail(ENDPOINT_ERROR);
        }
        if ((status & ((1u << 1) | (1u << 5))) == ((1u << 1) | (1u << 5))) break;
        uint32_t now = ticks();
        if ((uint32_t)(start - now) >= 3u * SYS_CLK_FREQ + cycles) {
            traffic_statistics();
            fail(ENDPOINT_TIMEOUT);
        }
        if ((uint32_t)(temperature_tick - now) >= SYS_CLK_FREQ / 10u) {
            refresh_temperatures();
            temperature_tick = now;
        }
        mb->heartbeat++;
    }
    /* DONE plus actual response retirement freezes the RTL counters. */
    traffic_statistics();
    mb->bus_errors_after = rd(CTRL_BUS_ERRORS);
    if (mb->bus_errors_before != mb->bus_errors_after) fail(BUS_ERROR);
    refresh_temperatures();
    mb->command_result = 0;
    mb->state = READY;
}

#ifdef HBM_ENDPOINT_TWO_PORT
static void traffic2_statistics(void) {
    mb->traffic2_status = rd(TF2_STATUS);
    mb->traffic2_errors = rd(TF2_ERRORS);
    mb->traffic2_cycles = rd(TF2_CYCLES);
    mb->traffic2_write_beats = rd(TF2_WRITE_BEATS);
    mb->traffic2_read_beats = rd(TF2_READ_BEATS);
    mb->traffic2_write_bursts = rd(TF2_WRITE_BURSTS);
    mb->traffic2_read_bursts = rd(TF2_READ_BURSTS);
    mb->traffic2_aw_count = rd(TF2_AW_COUNT);
    mb->traffic2_ar_count = rd(TF2_AR_COUNT);
    mb->traffic2_packed_w = rd(TF2_PACKED_W);
    mb->traffic2_packed_r = rd(TF2_PACKED_R);
    mb->traffic2_aw_stalls = rd(TF2_AW_STALLS);
    mb->traffic2_w_stalls = rd(TF2_W_STALLS);
    mb->traffic2_ar_stalls = rd(TF2_AR_STALLS);
    mb->traffic2_read_wait = rd(TF2_READ_WAIT);
    mb->traffic2_write_latency_min = rd(TF2_WRITE_LATENCY_MIN);
    mb->traffic2_write_latency_max = rd(TF2_WRITE_LATENCY_MAX);
    mb->traffic2_read_latency_min = rd(TF2_READ_LATENCY_MIN);
    mb->traffic2_read_latency_max = rd(TF2_READ_LATENCY_MAX);
    mb->traffic2_write_enabled_bytes_lo = rd(TF2_WRITE_ENABLED_BYTES_LO);
    mb->traffic2_write_enabled_bytes_hi = rd(TF2_WRITE_ENABLED_BYTES_HI);
    mb->traffic2_aw_native_beats = rd(TF2_AW_NATIVE_BEATS);
    mb->traffic2_ar_native_beats = rd(TF2_AR_NATIVE_BEATS);
    mb->traffic2_aw_packed_words = rd(TF2_AW_PACKED_WORDS);
    mb->traffic2_ar_packed_words = rd(TF2_AR_PACKED_WORDS);
    mb->traffic2_aw_length_seen = rd(TF2_AW_LENGTH_SEEN);
    mb->traffic2_ar_length_seen = rd(TF2_AR_LENGTH_SEEN);
}
static uint32_t traffic_pair_request_valid(uint32_t mode, uint32_t cycles,
        uint32_t seed, uint32_t random, uint32_t status,
        uint32_t seed_register, uint32_t random_register) {
    if (mode > 5 || random > 1 || cycles > (1u << 28) ||
            (mode >= 1 && mode <= 3 && !cycles) ||
            ((mode == 0 || mode >= 4) && cycles) ||
            (mode >= 4 && !random)) return 0;
    if ((status & 0xa8) != 0xa8 || (status & 0x11)) return 0;
    if (mode == 0 || (!random && mode == 1)) return 1;
    if (!(status & 4) || seed != rd(seed_register) || random != rd(random_register)) return 0;
    return mode != 4 || !(status & 0x200);
}
static void traffic_pair_run(void) {
    uint32_t mode = mb->traffic_mode, cycles = mb->traffic_cycles_limit;
    uint32_t seed = mb->traffic_seed, random = mb->traffic_random;
    uint32_t mode2 = mb->traffic2_mode, cycles2 = mb->traffic2_cycles_limit;
    uint32_t seed2 = mb->traffic2_seed, random2 = mb->traffic2_random;
    uint32_t status = rd(TF_STATUS), status2 = rd(TF2_STATUS);
    mb->command_result = 1;
    if (rd(TF_ERRORS) || rd(TF2_ERRORS) || ((status | status2) & 0x101)) fail(ENDPOINT_ERROR);
    /* Validate both requests and known images before launching either port. */
    if (!traffic_pair_request_valid(mode, cycles, seed, random, status,
            TF_CONFIG_SEED, TF_CONFIG_RANDOM) ||
            !traffic_pair_request_valid(mode2, cycles2, seed2, random2, status2,
            TF2_CONFIG_SEED, TF2_CONFIG_RANDOM)) return;
    refresh_temperatures();
    mb->traffic_status = 0; mb->traffic2_status = 0;
    mb->traffic_pair_elapsed_cycles = 0;
    mb->sequence++;
    mb->state = TESTING;
    mb->bus_errors_before = rd(CTRL_BUS_ERRORS);
    wr(TF_CONFIG_MODE, mode); wr(TF_CONFIG_CYCLES, cycles);
    wr(TF_CONFIG_SEED, seed); wr(TF_CONFIG_RANDOM, random);
    wr(TF2_CONFIG_MODE, mode2); wr(TF2_CONFIG_CYCLES, cycles2);
    wr(TF2_CONFIG_SEED, seed2); wr(TF2_CONFIG_RANDOM, random2);
    uint32_t start = ticks(), temperature_tick = start;
    wr(TF_START, 1); wr(TF2_START, 1);
    uint32_t limit = cycles > cycles2 ? cycles : cycles2;
    for (;;) {
        status = rd(TF_STATUS); status2 = rd(TF2_STATUS);
        check_trip();
        if (rd(TF_ERRORS) || rd(TF2_ERRORS) || ((status | status2) & 0x100)) {
            traffic_statistics(); traffic2_statistics(); fail(ENDPOINT_ERROR);
        }
        if ((status & 0x22) == 0x22 && (status2 & 0x22) == 0x22) break;
        uint32_t now = ticks();
        if ((uint32_t)(start - now) >= 3u * SYS_CLK_FREQ + limit) {
            traffic_statistics(); traffic2_statistics(); fail(ENDPOINT_TIMEOUT);
        }
        if ((uint32_t)(temperature_tick - now) >= SYS_CLK_FREQ / 10u) {
            refresh_temperatures(); temperature_tick = now;
        }
        mb->heartbeat++;
    }
    /* Include start skew, polling and both drains in the common SYS interval. */
    mb->traffic_pair_elapsed_cycles = (uint32_t)(start - ticks());
    /* Both engines have retired all responses before either snapshot is published. */
    traffic_statistics(); traffic2_statistics();
    mb->bus_errors_after = rd(CTRL_BUS_ERRORS);
    if (mb->bus_errors_before != mb->bus_errors_after) fail(BUS_ERROR);
    refresh_temperatures();
    mb->command_result = 0;
    mb->state = READY;
}
#endif
#endif

int main(void) {
    wr(TIMER_BASE + 8, 0); wr(TIMER_BASE, 0xffffffffu);
    wr(TIMER_BASE + 4, 0xffffffffu); wr(TIMER_BASE + 28, 0); wr(TIMER_BASE + 8, 1);
    for (uint32_t i = 0; i < 128; i++) ((volatile uint32_t *)mb)[i] = 0;
    mb->magic = 0x464b4842; mb->version = 3; mb->hbm_mhz = BOOT_HBM_MHZ; mb->sys_hz = SYS_CLK_FREQ;
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
#ifdef HBM_ENDPOINT_TRAFFIC
        if (command == 7 && mb->state == READY) traffic_run();
#ifdef HBM_ENDPOINT_TWO_PORT
        if (command == 8 && mb->state == READY) traffic_pair_run();
#endif
#endif
#if defined(HBM_ENDPOINT_PORT) && !defined(HBM_ENDPOINT_TRAFFIC)
        if (command == 6 && mb->state == READY) endpoint_transactions();
#endif
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
