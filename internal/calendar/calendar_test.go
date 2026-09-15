package calendar

import (
	"testing"
	"time"
)

func TestCalendarPeriods(t *testing.T) {
	loc, _ := time.LoadLocation("America/Los_Angeles")
	for _, tc := range []struct {
		reset string
		hours []int
	}{
		{"2026-09-19T05:00:00", []int{19, 24, 24, 24, 24, 24, 24, 5}},
		{"2026-09-19T00:00:00", []int{24, 24, 24, 24, 24, 24, 24}},
		{"2026-03-15T00:30:00", []int{0, 23, 24, 24, 24, 24, 24, 24, 0}},
		{"2026-11-07T05:00:00", []int{18, 25, 24, 24, 24, 24, 24, 5}},
	} {
		t.Run(tc.reset, func(t *testing.T) {
			reset, err := time.ParseInLocation("2006-01-02T15:04:05", tc.reset, loc)
			if err != nil {
				t.Fatal(err)
			}
			b := Bounds(reset.Unix(), loc)
			if len(b) != len(tc.hours) {
				t.Fatal(b)
			}
			total := int64(0)
			for i, d := range b {
				want := int64(tc.hours[i]) * 3600
				if len(b) == 9 && (i == 0 || i == 8) {
					want = 1800
				}
				if d.End-d.Start != want {
					t.Fatalf("interval %d = %d seconds, want %d", i, d.End-d.Start, want)
				}
				if i > 0 && (d.Start != b[i-1].End || time.Unix(d.Start, 0).In(loc).Hour() != 0) {
					t.Fatal("non-midnight boundary")
				}
				total += d.End - d.Start
			}
			if total != Week || b[0].Start != reset.Unix()-Week || b[len(b)-1].End != reset.Unix() {
				t.Fatal("period not covered")
			}
		})
	}
}
