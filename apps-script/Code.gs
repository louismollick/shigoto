function onEdit(e) {
  if (!e || !e.range) return;

  const sheet = e.range.getSheet();
  const row = e.range.getRow();
  const col = e.range.getColumn();

  if (row < 2) return;

  // Jobs to Review:
  // Column A = Set Status
  // Column B = Job ID
  if (sheet.getName() === 'Jobs to Review' && col === 1) {
    const status = e.value;
    const allowed = ['Not Applied', 'Applied', 'Skip'];

    if (!allowed.includes(status)) return;

    const jobId = sheet.getRange(row, 2).getDisplayValue().trim();

    // Clear the action cell before the formula view recalculates.
    e.range.clearContent();

    if (!jobId) return;

    const updates = {
      'Application Status': status
    };

    if (status === 'Applied') {
      updates['Applied At'] = new Date();
      updates['Application Stage'] = 'Applied';
    } else {
      updates['Applied At'] = '';
      updates['Application Stage'] = '';
    }

    updateShigotoJob_(e.source, jobId, updates);
    return;
  }

  // Applied:
  // A = Set Stage
  // B = Set Follow-up
  // C = Set Note
  // D = Job ID
  if (sheet.getName() === 'Applied' && [1, 2, 3].includes(col)) {
    const jobId = sheet.getRange(row, 4).getDisplayValue().trim();
    if (!jobId) return;

    const updates = {};

    if (col === 1) {
      const stage = e.value;
      const allowedStages = [
        'Applied',
        'Screening',
        'Interview',
        'Offer',
        'Rejected',
        'Withdrawn'
      ];

      if (!allowedStages.includes(stage)) return;
      updates['Application Stage'] = stage;
    }

    if (col === 2) {
      updates['Follow-up Date'] = e.range.getValue() || '';
    }

    if (col === 3) {
      updates['User Notes'] = e.value || '';
    }

    e.range.clearContent();
    updateShigotoJob_(e.source, jobId, updates);
  }
}


function updateShigotoJob_(ss, jobId, updates) {
  const sheet = ss.getSheetByName('Shigoto');

  if (!sheet || sheet.getLastRow() < 2) return;

  const headers = sheet
    .getRange(1, 1, 1, sheet.getLastColumn())
    .getDisplayValues()[0];

  const jobIdCol = headers.indexOf('Job ID') + 1;

  if (!jobIdCol) {
    throw new Error('Job ID column not found in Shigoto.');
  }

  const match = sheet
    .getRange(2, jobIdCol, sheet.getLastRow() - 1, 1)
    .createTextFinder(jobId)
    .matchEntireCell(true)
    .findNext();

  if (!match) {
    ss.toast(`Could not find Job ID ${jobId}`, 'Job Tracker', 5);
    return;
  }

  const targetRow = match.getRow();

  for (const [header, value] of Object.entries(updates)) {
    const targetCol = headers.indexOf(header) + 1;

    if (!targetCol) {
      throw new Error(`${header} column not found in Shigoto.`);
    }

    const cell = sheet.getRange(targetRow, targetCol);

    if (value === '') {
      cell.clearContent();
    } else {
      cell.setValue(value);

      if (header === 'Applied At') {
        cell.setNumberFormat('yyyy-mm-dd hh:mm');
      }

      if (header === 'Follow-up Date') {
        cell.setNumberFormat('yyyy-mm-dd');
      }
    }
  }

  SpreadsheetApp.flush();
}
