# Forward Window Recommendation Plan

## Goal

Evaluate lookback windows from 30 to 5 trading days for every available daily
parameter update from 2026-07-06 through 2026-08-06. Rank the windows using
their realized next-trading-day returns and recommend one production lookback.

## Data Rules

- A run dated `T` trains only on signal days whose strict next-day returns are
  available on or before `T`.
- The optimized settings then select at most one stock using factor data dated
  `T`; its strict return from `T` to the following market day is the
  out-of-sample outcome.
- If the following market day's return is unavailable, the run is retained as
  `awaiting_settlement` and excluded from performance ranking.
- Each lookback window has its own chronological parameter chain. It starts
  from the program defaults and each successful daily result seeds only the
  next date for the same lookback.

## Files

- Create `forward_window_recommendation.py`: perform resumable rolling
  optimization, write one compact JSON result per date/window, and generate
  the ranking, top-three, daily-detail, failure, and recommendation outputs.
- Create `test_forward_window_recommendation.py`: test the settlement cutoff
  and compounding-based ranking without market-data or network dependencies.
- Update `README.md`: document the command, the no-leakage timing rule, and
  output files.

## Execution

1. Write and run the new tests to confirm they fail because the module does
   not yet exist.
2. Implement the date selection and aggregation helpers until the tests pass.
3. Implement the rolling optimizer runner. Reuse the daily optimizer's
   coordinate search and selection code, while keeping all writes below
   `parameter_window_analysis/forward_recommendation_results/`.
4. Run the full 30-to-5, 2026-07-06-to-2026-08-06 analysis with four isolated
   lookback-chain workers. Restarting the command reuses completed JSON files.
5. Rebuild outputs from stored results, run the test suite and compilation
   checks, then report the top three and recommended window.
