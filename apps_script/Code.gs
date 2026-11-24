// Job Search Digest helper: records one-click feedback from the digest
// email, adds applied roles to the Applied tab, and sends the digest.
// Paste into Extensions > Apps Script of your application-tracker Sheet,
// then Deploy > New deployment > Web app (Execute as: Me, Access: Anyone).

const TOKEN = 'REPLACE_WITH_A_LONG_RANDOM_STRING';
const SHEET_ID = 'REPLACE_WITH_YOUR_SHEET_ID';
const APPLIED_TAB = 'Applied';
const FEEDBACK_TAB = 'Digest Feedback';
const TO = 'you@example.com';
const ROLE_KEEP_DAYS = 30;
const FEEDBACK_HEADER = ['Date', 'Verdict', 'Reason', 'Title', 'Company', 'Sector',
                         'Location', 'Source', 'Link', 'Role ID', 'Note'];
const REASONS = {
  senior: 'Too senior',
  role: 'Wrong type of role',
  industry: 'Not this industry',
  small: 'Company too small',
  location: 'Location',
  company: 'Not this company',
};

function doGet(e) {
  const p = (e && e.parameter) || {};
  if (p.t !== TOKEN) return page_('This link isn\u2019t valid.', '');
  if (p.a === 'ping') return json_({ok: true});
  const lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    if (p.a === 'x') return json_(exportData_());
    if (p.a === 'y') return markApplied_(p.i);
    if (p.a === 'n') return markSkip_(p.i);
    if (p.a === 'r') return setReason_(p.i, p.r);
    if (p.a === 'o') return setNote_(p.i, p.note);
    if (p.a === 'u') return undo_(p.i);
    return page_('Unknown action.', '');
  } finally {
    lock.releaseLock();
  }
}

function doPost(e) {
  const body = JSON.parse(e.postData.contents);
  if (body.t !== TOKEN) return json_({ok: false, error: 'bad token'});
  if (body.action !== 'send') return json_({ok: false, error: 'unknown action'});
  saveRoles_(body.roles || []);
  MailApp.sendEmail({to: TO, subject: body.subject, htmlBody: body.html});
  return json_({ok: true});
}

// ---------- role registry (details behind each role ID in the email) ----------

function saveRoles_(roles) {
  const props = PropertiesService.getScriptProperties();
  const today = new Date().toISOString().slice(0, 10);
  const updates = {};
  roles.forEach(function (r) {
    r.seen = today;
    updates['r:' + r.id] = JSON.stringify(r);
  });
  props.setProperties(updates);
  const cutoff = new Date(Date.now() - ROLE_KEEP_DAYS * 86400000).toISOString().slice(0, 10);
  const all = props.getProperties();
  Object.keys(all).forEach(function (k) {
    if (k.indexOf('r:') !== 0) return;
    try {
      if (JSON.parse(all[k]).seen < cutoff) props.deleteProperty(k);
    } catch (err) {
      props.deleteProperty(k);
    }
  });
}

function getRole_(id) {
  const raw = PropertiesService.getScriptProperties().getProperty('r:' + id);
  return raw ? JSON.parse(raw) : null;
}

// ---------- actions ----------

function markApplied_(id) {
  const role = getRole_(id);
  if (!role) return missing_();
  const existing = feedbackRows_(id);
  if (existing.some(function (r) { return r.verdict === 'Applied'; })) {
    return page_('Already marked as applied: ' + label_(role) + '.', undoLink_(id));
  }
  clearFeedback_(id);
  appendFeedback_(role, 'Applied', '');
  const ss = ss_();
  const sheet = ss.getSheetByName(APPLIED_TAB);
  if (appliedRowExists_(sheet, role)) {
    return page_('\u2713 Marked as applied: ' + label_(role) +
                 '. It was already on your Applied tab, so nothing new was added there.', undoLink_(id));
  }
  // New row copies the previous row's formatting and Status dropdown, and
  // gets a date-only value, so it matches the rows typed by hand.
  const prev = sheet.getLastRow();
  const row = prev + 1;
  const cols = sheet.getLastColumn();
  if (prev >= 2) {
    const src = sheet.getRange(prev, 1, 1, cols);
    const dest = sheet.getRange(row, 1, 1, cols);
    src.copyTo(dest, SpreadsheetApp.CopyPasteType.PASTE_FORMAT, false);
    src.copyTo(dest, SpreadsheetApp.CopyPasteType.PASTE_DATA_VALIDATION, false);
  }
  const today = Utilities.formatDate(new Date(), ss.getSpreadsheetTimeZone(), 'M/d/yyyy');
  sheet.getRange(row, 1, 1, 6).setValues([['', role.title, role.company, 'Yes', today, 'Sent']]);
  sheet.getRange(row, 5).setNumberFormat('M/d/yyyy');
  // Location goes in whichever column is headed "Location" (column H today).
  const headers = sheet.getRange(1, 1, 1, cols).getValues()[0]
    .map(function (h) { return String(h).trim().toLowerCase(); });
  const locCol = headers.indexOf('location') + 1;
  const loc = role.sheet_location || role.location || '';
  if (locCol > 0 && loc) sheet.getRange(row, locCol).setValue(loc);
  const rt = SpreadsheetApp.newRichTextValue().setText(role.title).setLinkUrl(role.url).build();
  sheet.getRange(row, 2).setRichTextValue(rt);
  return page_('\u2713 Marked as applied: ' + label_(role) + '. Added to your Applied tab.',
               undoLink_(id));
}

function markSkip_(id) {
  const role = getRole_(id);
  if (!role) return missing_();
  clearFeedback_(id);
  removeAppliedRow_(role);
  appendFeedback_(role, 'Not for me', '');
  const base = ScriptApp.getService().getUrl();
  const chips = Object.keys(REASONS).map(function (k) {
    const href = base + '?a=r&i=' + encodeURIComponent(id) + '&r=' + k + '&t=' + TOKEN;
    return '<a class="chip" target="_top" href="' + href + '">' + REASONS[k] + '</a>';
  }).join('');
  return page_('Got it, hidden: ' + label_(role) + '.',
               '<p class="sub">Want to tell me why? Optional, one tap:</p><div>' + chips + '</div>' +
               noteForm_(id) + undoLink_(id));
}

function setReason_(id, key) {
  const role = getRole_(id);
  const reason = REASONS[key];
  if (!role || !reason) return missing_();
  const sheet = feedbackSheet_();
  const rows = feedbackRows_(id).filter(function (r) { return r.verdict === 'Not for me'; });
  if (!rows.length) appendFeedback_(role, 'Not for me', reason);
  rows.forEach(function (r) { sheet.getRange(r.row, 3).setValue(reason); });
  const extra = (key === 'company' || key === 'small')
    ? ' I\u2019ll stop showing roles from ' + esc_(role.company) + '.'
    : ' I\u2019ll use that to tune future digests.';
  return page_('Thanks, noted: ' + esc_(reason) + '.' + extra, noteForm_(id) + undoLink_(id));
}

function setNote_(id, note) {
  const role = getRole_(id);
  if (!role) return missing_();
  note = String(note || '').trim().slice(0, 300);
  if (!note) return page_('Nothing to save: the note was empty.', noteForm_(id) + undoLink_(id));
  const sheet = feedbackSheet_();
  let rows = feedbackRows_(id).filter(function (r) { return r.verdict === 'Not for me'; });
  if (!rows.length) {
    appendFeedback_(role, 'Not for me', '');
    rows = feedbackRows_(id).filter(function (r) { return r.verdict === 'Not for me'; });
  }
  rows.forEach(function (r) { sheet.getRange(r.row, 11).setValue(note); });
  return page_('Thanks, saved your note: \u201c' + esc_(note) + '\u201d', undoLink_(id));
}

function noteForm_(id) {
  return '<form class="note" target="_top" method="get" action="' + ScriptApp.getService().getUrl() + '">' +
    '<input type="hidden" name="a" value="o"><input type="hidden" name="i" value="' + esc_(id) + '">' +
    '<input type="hidden" name="t" value="' + TOKEN + '">' +
    '<input type="text" name="note" maxlength="300" placeholder="Other reason? Type it here">' +
    '<button type="submit">Save</button></form>';
}

function undo_(id) {
  const role = getRole_(id);
  if (!role) return missing_();
  clearFeedback_(id);
  removeAppliedRow_(role);
  return page_('Undone. ' + label_(role) + ' will show up again in your next digest.', '');
}

// ---------- sheet helpers ----------

function ss_() { return SpreadsheetApp.openById(SHEET_ID); }

function feedbackSheet_() {
  const ss = ss_();
  let sheet = ss.getSheetByName(FEEDBACK_TAB);
  if (!sheet) {
    sheet = ss.insertSheet(FEEDBACK_TAB);
    sheet.appendRow(FEEDBACK_HEADER);
    sheet.getRange(1, 1, 1, FEEDBACK_HEADER.length).setFontWeight('bold');
    sheet.setFrozenRows(1);
  } else if (sheet.getRange(1, FEEDBACK_HEADER.length).getValue() !== 'Note') {
    sheet.getRange(1, FEEDBACK_HEADER.length).setValue('Note').setFontWeight('bold');
  }
  return sheet;
}

function appendFeedback_(role, verdict, reason) {
  feedbackSheet_().appendRow([new Date(), verdict, reason, role.title, role.company,
                              role.sector || '', role.location || '', role.source || '',
                              role.url || '', role.id, '']);
}

function feedbackRows_(id) {
  const sheet = feedbackSheet_();
  const last = sheet.getLastRow();
  if (last < 2) return [];
  const values = sheet.getRange(2, 1, last - 1, FEEDBACK_HEADER.length).getValues();
  const out = [];
  values.forEach(function (v, i) {
    if (String(v[9]) === String(id)) out.push({row: i + 2, verdict: v[1]});
  });
  return out;
}

function clearFeedback_(id) {
  const sheet = feedbackSheet_();
  feedbackRows_(id).reverse().forEach(function (r) { sheet.deleteRow(r.row); });
}

function norm_(s) { return String(s || '').toLowerCase().replace(/[^a-z0-9]/g, ''); }

function appliedRowExists_(sheet, role) {
  const last = sheet.getLastRow();
  if (last < 2) return false;
  const t = norm_(role.title), c = norm_(role.company);
  return sheet.getRange(2, 2, last - 1, 2).getDisplayValues().some(function (v) {
    const vc = norm_(v[1]);
    const sameCompany = vc && c && (vc === c || vc.indexOf(c) >= 0 || c.indexOf(vc) >= 0);
    return sameCompany && norm_(v[0]) === t;
  });
}

function removeAppliedRow_(role) {
  if (!role.url) return;
  const sheet = ss_().getSheetByName(APPLIED_TAB);
  const last = sheet.getLastRow();
  if (last < 2) return;
  const links = sheet.getRange(2, 2, last - 1, 1).getRichTextValues();
  for (let i = links.length - 1; i >= 0; i--) {
    const rt = links[i][0];
    if (rt && rt.getLinkUrl() === role.url) sheet.deleteRow(i + 2);
  }
}

function exportData_() {
  const fb = feedbackSheet_();
  const fbLast = fb.getLastRow();
  const feedback = fbLast < 2 ? [] :
    fb.getRange(2, 1, fbLast - 1, FEEDBACK_HEADER.length).getDisplayValues().map(function (v) {
      return {date: v[0], verdict: v[1], reason: v[2], title: v[3], company: v[4],
              sector: v[5], location: v[6], source: v[7], url: v[8], id: v[9], note: v[10]};
    });
  const ap = ss_().getSheetByName(APPLIED_TAB);
  const apLast = ap.getLastRow();
  const applied = apLast < 2 ? [] :
    ap.getRange(2, 2, apLast - 1, 2).getDisplayValues()
      .filter(function (v) { return v[0] || v[1]; })
      .map(function (v) { return {title: v[0], company: v[1]}; });
  return {feedback: feedback, applied: applied};
}

// ---------- output helpers ----------

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

function esc_(s) {
  return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function label_(role) {
  return '<b>' + esc_(role.title) + '</b> at ' + esc_(role.company);
}

function undoLink_(id) {
  const href = ScriptApp.getService().getUrl() + '?a=u&i=' + encodeURIComponent(id) + '&t=' + TOKEN;
  return '<p class="undo"><a target="_top" href="' + href + '">Undo</a></p>';
}

function missing_() {
  return page_('Couldn\u2019t find that role. It may be from a digest older than ' +
               ROLE_KEEP_DAYS + ' days.', '');
}

function page_(message, extraHtml) {
  const html =
    '<style>body{font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;' +
    'color:#111827;max-width:520px;margin:40px auto;padding:0 16px;line-height:1.5}' +
    '.msg{font-size:17px}.sub{color:#6B7280;font-size:14px;margin-top:22px}' +
    '.chip{display:inline-block;border:1px solid #D1D5DB;border-radius:999px;padding:7px 13px;' +
    'margin:4px 6px 4px 0;color:#111827;text-decoration:none;font-size:14px}' +
    '.chip:hover{border-color:#111827}.undo{margin-top:26px;font-size:13px}' +
    '.undo a{color:#6B7280}.note{margin-top:14px;display:flex;gap:8px}' +
    '.note input[type=text]{flex:1;font-size:14px;padding:7px 10px;border:1px solid #D1D5DB;border-radius:8px}' +
    '.note button{font-size:14px;padding:7px 14px;border:1px solid #111827;border-radius:8px;' +
    'background:#111827;color:#fff;cursor:pointer}</style>' +
    '<p class="msg">' + message + '</p>' + (extraHtml || '');
  return HtmlService.createHtmlOutput(html).setTitle('Job Search Digest')
    .addMetaTag('viewport', 'width=device-width, initial-scale=1');
}
