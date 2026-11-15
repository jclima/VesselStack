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
  VS_BANNER_ALARM = 1,    // at least one counted alarm is live
  VS_BANNER_NO_DATA = 2,  // every counted alarm is lost: its data stopped while it was in alarm
};

// What the banner shows. The banner is visible only for a fresh summary, a working link, an
// alarm and no dismissal. na counts live and lost alarms, nl the lost ones among them; a
// bridge older than 0.9.0 sends no nl, which arrives here as 0 and gives the alarm wording.
static inline int vs_banner_mode(bool fresh, bool link_ok, uint8_t max, int na, int nl, bool dismissed) {
  if (!(fresh && link_ok && max == 3 && na > 0 && !dismissed))
    return VS_BANNER_HIDDEN;
  return nl > 0 && nl >= na ? VS_BANNER_NO_DATA : VS_BANNER_ALARM;
}

// The count line: "<n> ALARM(S)" or "NO DATA" / "<n> NO DATA". More than 99 shows "99+".
static inline void vs_banner_count(char *buf, size_t size, int mode, int na) {
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
