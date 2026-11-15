// Host test for displays/esphome/app/vesselstack_logic.h. Built and run by tests/test_displays_logic.py.
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "vesselstack_logic.h"

static int failures = 0;
static int checks = 0;

#define CHECK(cond)                                                       \
  do {                                                                    \
    checks++;                                                             \
    if (!(cond)) {                                                        \
      failures++;                                                         \
      printf("FAIL line %d: %s\n", __LINE__, #cond);                      \
    }                                                                     \
  } while (0)

// Receiver state, as the firmware keeps it between summaries.
struct State {
  bool valid = false, has_seq = false;
  uint32_t seq = 0;
  uint8_t max = 0;
  int na = 0;
};

// Feed one summary the way the message handler does; returns "rising". seq < 0 means no seq field.
static bool feed(State &s, uint8_t m, int na, int64_t seq) {
  bool has = seq >= 0;
  bool rising = vs_alarm_rising(!s.valid, m, na, has, has ? (uint32_t) seq : 0, s.has_seq, s.seq, s.max, s.na);
  s.max = m;
  s.na = na;
  s.has_seq = has;
  s.seq = has ? (uint32_t) seq : 0;
  s.valid = true;
  return rising;
}

static void rising_with_seq() {
  State s;
  CHECK(feed(s, 3, 1, 10));    // first summary after boot with an alarm shows the banner
  CHECK(!feed(s, 3, 1, 10));   // the same summary again (heartbeat)
  CHECK(!feed(s, 3, 1, 10));   // link returns after a stale period, nothing changed
  CHECK(feed(s, 3, 1, 11));    // A cleared, B raised: same max and na, new seq
  CHECK(feed(s, 3, 2, 12));    // one more alarm
  CHECK(!feed(s, 3, 1, 12));   // one alarm cleared: no new alarm
  CHECK(!feed(s, 3, 1, 12));   // alarm turned lost (data stopped): same seq
  CHECK(!feed(s, 2, 0, 12));   // only warnings
  CHECK(feed(s, 3, 1, 13));    // a new alarm after a quiet period
  CHECK(feed(s, 3, 1, 0x7fffffffu + 5));   // a random new value
}

static void rising_first_summary() {
  State a;
  CHECK(!feed(a, 1, 0, 5));    // ok on boot: nothing to raise
  CHECK(feed(a, 3, 1, 6));
  State b;
  CHECK(!feed(b, 2, 3, 5));    // warnings only
  State c;
  CHECK(feed(c, 3, 4, 0));     // seq 0 is a normal value
}

static void rising_seq_wrap_and_restart() {
  State s;
  CHECK(feed(s, 3, 1, 0xFFFFFFFFll));
  CHECK(!feed(s, 3, 1, 0xFFFFFFFFll));
  CHECK(feed(s, 3, 2, 0));     // wrapped
  CHECK(!feed(s, 3, 2, 0));
  CHECK(feed(s, 3, 2, 0xFFFFFFFFll));   // a smaller or larger value is only "different"
  CHECK(feed(s, 3, 2, 7));     // bridge restarted at a random value
}

static void rising_without_seq() {
  State s;
  CHECK(feed(s, 3, 1, -1));    // first
  CHECK(!feed(s, 3, 1, -1));   // unchanged: a reconnect no longer re-raises
  CHECK(!feed(s, 3, 1, -1));
  CHECK(feed(s, 3, 2, -1));    // count grew
  CHECK(!feed(s, 3, 1, -1));   // count fell
  CHECK(!feed(s, 1, 0, -1));
  CHECK(feed(s, 3, 1, -1));    // severity back to alarm
}

static void rising_bridge_upgrade() {
  State s;
  CHECK(feed(s, 3, 1, -1));
  CHECK(feed(s, 3, 1, 5));     // first message with a seq after one without: treated as new
  CHECK(!feed(s, 3, 1, 5));
}

static int mode_of(uint8_t max, int na, int nl) { return vs_banner_mode(true, true, true, max, na, nl, false); }

static void mode_table() {
  CHECK(mode_of(3, 1, 0) == VS_BANNER_ALARM);        // one live alarm
  CHECK(mode_of(3, 3, 0) == VS_BANNER_ALARM);
  CHECK(mode_of(3, 1, 1) == VS_BANNER_NO_DATA);      // one lost alarm
  CHECK(mode_of(3, 4, 4) == VS_BANNER_NO_DATA);      // all lost
  CHECK(mode_of(3, 2, 1) == VS_BANNER_ALARM);        // one live, one lost: still an alarm
  CHECK(mode_of(3, 5, 4) == VS_BANNER_ALARM);
  CHECK(mode_of(3, 2, 5) == VS_BANNER_NO_DATA);      // nl > na: treated as all lost
  CHECK(mode_of(3, 2, 0) == VS_BANNER_ALARM);        // nl missing reads as 0: the old wording
  CHECK(mode_of(3, 2, -3) == VS_BANNER_ALARM);       // nonsense nl never means lost
  CHECK(mode_of(3, 0, 0) == VS_BANNER_HIDDEN);       // no alarm counted
  CHECK(mode_of(3, 0, 2) == VS_BANNER_HIDDEN);
  CHECK(mode_of(2, 3, 3) == VS_BANNER_HIDDEN);       // only warnings
  CHECK(mode_of(1, 0, 0) == VS_BANNER_HIDDEN);
  CHECK(mode_of(0, 0, 0) == VS_BANNER_HIDDEN);
  // link lost or summary stale: the last known alarm turns into NO DATA, it never disappears
  CHECK(vs_banner_mode(true, false, true, 3, 1, 0, false) == VS_BANNER_LINK_LOST);   // summary stale
  CHECK(vs_banner_mode(true, true, false, 3, 1, 0, false) == VS_BANNER_LINK_LOST);   // link down
  CHECK(vs_banner_mode(true, false, false, 3, 2, 1, false) == VS_BANNER_LINK_LOST);  // both, one lost
  CHECK(vs_banner_mode(true, false, false, 3, 1, 1, false) == VS_BANNER_LINK_LOST);  // lost alarm, link lost
  CHECK(vs_banner_mode(true, false, true, 2, 3, 0, false) == VS_BANNER_HIDDEN);      // last summary only warnings
  CHECK(vs_banner_mode(true, true, false, 1, 0, 0, false) == VS_BANNER_HIDDEN);      // last summary ok
  CHECK(vs_banner_mode(true, false, false, 3, 0, 0, false) == VS_BANNER_HIDDEN);     // no alarm counted
  CHECK(vs_banner_mode(false, false, false, 3, 1, 0, false) == VS_BANNER_HIDDEN);    // never valid, link down
  CHECK(vs_banner_mode(false, true, true, 3, 1, 0, false) == VS_BANNER_HIDDEN);      // never valid
  CHECK(vs_banner_mode(true, true, true, 3, 1, 0, true) == VS_BANNER_HIDDEN);        // dismissed
  CHECK(vs_banner_mode(true, true, true, 3, 1, 1, true) == VS_BANNER_HIDDEN);        // dismissed, lost
  CHECK(vs_banner_mode(true, false, true, 3, 1, 0, true) == VS_BANNER_HIDDEN);       // dismissed, then stale
  CHECK(vs_banner_mode(true, true, false, 3, 1, 0, true) == VS_BANNER_HIDDEN);       // dismissed, then link lost
}

static void count_text() {
  char buf[16];
  vs_banner_count(buf, sizeof(buf), VS_BANNER_ALARM, 1);
  CHECK(strcmp(buf, "1 ALARM") == 0);
  vs_banner_count(buf, sizeof(buf), VS_BANNER_ALARM, 2);
  CHECK(strcmp(buf, "2 ALARMS") == 0);
  vs_banner_count(buf, sizeof(buf), VS_BANNER_ALARM, 99);
  CHECK(strcmp(buf, "99 ALARMS") == 0);
  vs_banner_count(buf, sizeof(buf), VS_BANNER_ALARM, 100);
  CHECK(strcmp(buf, "99+ ALARMS") == 0);
  vs_banner_count(buf, sizeof(buf), VS_BANNER_NO_DATA, 1);
  CHECK(strcmp(buf, "NO DATA") == 0);
  vs_banner_count(buf, sizeof(buf), VS_BANNER_NO_DATA, 2);
  CHECK(strcmp(buf, "2 NO DATA") == 0);
  vs_banner_count(buf, sizeof(buf), VS_BANNER_NO_DATA, 99);
  CHECK(strcmp(buf, "99 NO DATA") == 0);
  vs_banner_count(buf, sizeof(buf), VS_BANNER_NO_DATA, 100);
  CHECK(strcmp(buf, "99+ NO DATA") == 0);
  vs_banner_count(buf, sizeof(buf), VS_BANNER_NO_DATA, 100000);
  CHECK(strcmp(buf, "99+ NO DATA") == 0);
  vs_banner_count(buf, sizeof(buf), VS_BANNER_LINK_LOST, 1);
  CHECK(strcmp(buf, "NO DATA") == 0);       // the count is unknown: never a number
  vs_banner_count(buf, sizeof(buf), VS_BANNER_LINK_LOST, 7);
  CHECK(strcmp(buf, "NO DATA") == 0);
  vs_banner_count(buf, sizeof(buf), VS_BANNER_LINK_LOST, 100000);
  CHECK(strcmp(buf, "NO DATA") == 0);
  vs_banner_count(buf, sizeof(buf), VS_BANNER_ALARM, 2147483647);
  CHECK(strcmp(buf, "99+ ALARMS") == 0);
}

// An alarm, the link is lost and returns: the dismissal and the page are not touched by the loss,
// and only a new seq brings the banner back.
static void link_loss_sequence() {
  State s;
  bool dismissed = false;
  CHECK(feed(s, 3, 1, 20));                                                          // alarm raised
  CHECK(vs_banner_mode(s.valid, true, true, s.max, s.na, 0, dismissed) == VS_BANNER_ALARM);
  CHECK(vs_banner_mode(s.valid, false, false, s.max, s.na, 0, dismissed) == VS_BANNER_LINK_LOST);
  dismissed = true;                                                                  // tap during the loss
  CHECK(vs_banner_mode(s.valid, false, false, s.max, s.na, 0, dismissed) == VS_BANNER_HIDDEN);
  CHECK(!feed(s, 3, 1, 20));                                                         // link back, same seq
  CHECK(vs_banner_mode(s.valid, true, true, s.max, s.na, 0, dismissed) == VS_BANNER_HIDDEN);
  bool rising = feed(s, 3, 1, 21);                                                   // a new alarm
  CHECK(rising);
  if (rising) dismissed = false;
  CHECK(vs_banner_mode(s.valid, true, true, s.max, s.na, 0, dismissed) == VS_BANNER_ALARM);
  CHECK(!feed(s, 1, 0, 21));                                                         // alarm cleared while the link was down
  CHECK(vs_banner_mode(s.valid, true, true, s.max, s.na, 0, dismissed) == VS_BANNER_HIDDEN);
}

static void unit_tag_table() {
  CHECK(vs_unit_ok("", "1a2b3c4d"));              // a bridge older than 0.9.0 sends no cu
  CHECK(vs_unit_ok(NULL, "1a2b3c4d"));
  CHECK(vs_unit_ok("1a2b3c4d", "1a2b3c4d"));      // equal
  CHECK(!vs_unit_ok("1a2b3c4e", "1a2b3c4d"));     // different
  CHECK(!vs_unit_ok("1a2b3c4", "1a2b3c4d"));      // shorter: a prefix is not a match
  CHECK(!vs_unit_ok("1a2b3c4d0", "1a2b3c4d"));    // longer
  CHECK(!vs_unit_ok("1A2B3C4D", "1a2b3c4d"));     // case matters
  CHECK(!vs_unit_ok(" ", "1a2b3c4d"));            // not empty
  CHECK(!vs_unit_ok("x", "1a2b3c4d"));
}

int main() {
  rising_with_seq();
  rising_first_summary();
  rising_seq_wrap_and_restart();
  rising_without_seq();
  rising_bridge_upgrade();
  mode_table();
  count_text();
  link_loss_sequence();
  unit_tag_table();
  printf("%d checks, %d failures\n", checks, failures);
  return failures == 0 ? 0 : 1;
}
