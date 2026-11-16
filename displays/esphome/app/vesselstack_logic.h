// VesselStack alarm banner decisions.
//
// Pure C++ with no ESPHome or Arduino includes, so the same code is compiled into the
// firmware and tested on a host (tests/test_displays_logic.py). The generated lambdas in
// the device YAML call these functions; keep every banner decision here.
#pragma once

#include <stdint.h>
#include <stdio.h>
#include <string.h>

// True when a new summary should raise the banner again and undo a dismissal.
//
// m is the highest severity (3 = alarm), na the number of alarms, has_seq/seq the summary's
// counter (the bridge raises it for every new live alarm; it wraps and may restart at any
// value, so it is only ever compared for inequality). first is true for the first summary
// since boot. prev_* is the last summary received, kept while the link is down or the summary
// is stale, so a reconnect with an unchanged summary is never rising.
static inline bool vs_alarm_rising(bool first, uint8_t m, int na, bool has_seq, uint32_t seq, bool prev_has_seq,
                                   uint32_t prev_seq, uint8_t prev_max, int prev_na) {
  if (m != 3)
    return false;
  if (first)
    return true;
  if (has_seq)
    return !prev_has_seq || seq != prev_seq;
  return prev_max != 3 || na > prev_na;  // a bridge older than 0.9.0 sends no seq
}

enum VsBannerMode : int {
  VS_BANNER_HIDDEN = 0,
  VS_BANNER_ALARM = 1,      // at least one counted alarm is live
  VS_BANNER_NO_DATA = 2,    // every counted alarm is lost: its data stopped while it was in alarm
  VS_BANNER_LINK_LOST = 3,  // the last known summary was an alarm, but the summary is stale or the link is down
};

// What the banner shows. An alarm the display has seen is never cleared by losing data or the
// link: it turns into NO DATA until the bridge reports again, the alarm is dismissed, or a fresh
// summary says it is over.
//   valid      a summary has arrived since boot. The broker keeps the last summary (retained), so a display
//              that boots while the bridge is down still gets it: an alarm in it shows as NO DATA until
//              a tap or a fresh summary. Only a broker with no stored summary leaves the display blank.
//   fresh      the last summary is recent
//   link_ok    the bridge link is live
//   na, nl     alarms counted, and the lost ones among them; a bridge older than 0.9.0 sends no nl,
//              which arrives here as 0 and gives the alarm wording
// Losing the link is not a new alarm: vs_alarm_rising decides that, and dismissed stays as it is.
static inline int vs_banner_mode(bool valid, bool fresh, bool link_ok, uint8_t max, int na, int nl, bool dismissed) {
  if (!valid || dismissed || max != 3 || na <= 0)
    return VS_BANNER_HIDDEN;
  if (!(fresh && link_ok))
    return VS_BANNER_LINK_LOST;
  return nl > 0 && nl >= na ? VS_BANNER_NO_DATA : VS_BANNER_ALARM;
}

// The count line: "<n> ALARM(S)", "NO DATA" / "<n> NO DATA", or plain "NO DATA" when the link is lost
// (the count is unknown then). More than 99 shows "99+".
static inline void vs_banner_count(char *buf, size_t size, int mode, int na) {
  if (mode == VS_BANNER_LINK_LOST) {
    snprintf(buf, size, "NO DATA");
    return;
  }
  const char *plus = na > 99 ? "+" : "";
  int shown = na > 99 ? 99 : na;
  if (mode == VS_BANNER_NO_DATA) {
    if (na == 1)
      snprintf(buf, size, "NO DATA");
    else
      snprintf(buf, size, "%d%s NO DATA", shown, plus);
    return;
  }
  snprintf(buf, size, "%d%s %s", shown, plus, na == 1 ? "ALARM" : "ALARMS");
}

// True when the unit tag in an instrument payload matches the one the firmware was built for.
// The bridge adds cu (first 8 hex characters of SHA-256 over convert|unit|precision); a
// mismatch means the number is in another unit than the label says. A payload without cu comes
// from a bridge older than 0.9.0 and is accepted.
static inline bool vs_unit_ok(const char *cu, const char *expected) {
  if (cu == NULL || cu[0] == '\0')
    return true;
  return strcmp(cu, expected) == 0;
}
