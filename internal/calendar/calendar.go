// Package calendar partitions a fixed quota period into local calendar dates.
package calendar

import "time"

const Week = int64(604800)
const MaxDays = 9

type Interval struct{ Start, End int64 }

func Bounds(end int64, loc *time.Location) []Interval {
	out := make([]Interval, 0, MaxDays)
	for start := end - Week; start < end; {
		t := time.Unix(start, 0).In(loc)
		y, m, d := t.Date()
		next := time.Date(y, m, d+1, 0, 0, 0, 0, loc)
		// Some zones advance at midnight. Date may resolve the missing time on
		// the preceding date; walk to the first valid minute on the next date.
		for i := 0; i < 180 && next.Format("2006-01-02") <= t.Format("2006-01-02"); i++ {
			next = next.Add(time.Minute)
		}
		stop := min(end, next.Unix())
		if stop <= start || len(out) == MaxDays {
			return nil
		}
		out = append(out, Interval{start, stop})
		start = stop
	}
	if len(out) < 7 {
		return nil
	}
	return out
}
