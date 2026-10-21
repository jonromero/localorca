# Contributing

LocalOrca lets a local model change real code unattended, so its safety rules matter more than its features.

1. **Every bug fix comes with a regression test** in `tests/` (`test_guards.py` for the build loop, `test_design.py` for the design stage and the dashboard) that fails without the fix.
   The tests use a fake LM Studio and a fake `claude`, so they run anywhere in seconds:

       python3 -m unittest discover -s tests -v

2. **Never weaken a guard to make a build pass.** If a guard blocks something legitimate, make the guard
   smarter and add a test for both the good and the bad case.
3. Python standard library only. No dependencies.

The rules the tests protect, and the real failure behind each one, are listed in the README under "Safety rules".
