# Google Sheets script

`Code.gs` is the supplied spreadsheet-bound Apps Script, preserved without behavior changes. Git stores its history; pushing this repository does not update the live Apps Script project. To deploy a change, paste the file into the spreadsheet's Extensions > Apps Script editor and save.

`Jobs to Review` and `Applied` are formula views. Their action cells write to `Shigoto` by Job ID and then clear themselves. The current `Jobs to Review` formula excludes Applied and Skip jobs. The Applied tab only edits stage, follow-up date and notes, so it cannot reverse application status.

`Closed` is a read-only formula view of all 24 current Shigoto columns. Its formulas are:

- A1: `=ARRAYFORMULA(Shigoto!A1:X1)`
- A2: `=IFNA(FILTER(Shigoto!A2:X,Shigoto!M2:M="Closed"),"")`

If reviewer columns are added beyond X, widen both formulas. The app rebuilds only Shigoto, so Closed's formulas persist across syncs. Reviewer edits belong in Shigoto.
