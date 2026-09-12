from __future__ import annotations

import json
import subprocess
from pathlib import Path


H5_WORKBENCH = Path("apps/h5/public/v12-workbench.js")


def test_partial_screenshot_upload_waits_for_batch_before_unlocking_submit() -> None:
    source = H5_WORKBENCH.read_text(encoding="utf-8")
    start = source.index("async function uploadEvidenceBatch")
    end = source.index("\nfunction evidence", start)
    helpers = source[start:end]
    scenario = f"""
{helpers}
const submitButton = {{disabled: true}};
const uploadedTypes = new Set();
let uploading = true;
let releaseSecond;
let markSecondStarted;
const secondStarted = new Promise(resolve => {{ markSecondStarted = resolve; }});
const events = [];
syncEvidenceSubmitButton(submitButton, uploadedTypes, uploading);
const batch = uploadEvidenceBatch(
  [
    {{file: {{name: 'first.png'}}, type: 'CHAT_SCREENSHOT'}},
    {{file: {{name: 'second.png'}}, type: 'CHAT_SCREENSHOT'}},
  ],
  async file => {{
    events.push(`upload:${{file.name}}`);
    if (file.name === 'second.png') {{
      markSecondStarted();
      await new Promise(resolve => {{ releaseSecond = resolve; }});
      throw new Error('文件过大');
    }}
  }},
  (file, type) => {{
    events.push(`saved:${{file.name}}`);
    uploadedTypes.add(type);
  }},
);
await secondStarted;
const disabledDuringSecondUpload = submitButton.disabled;
releaseSecond();
const result = await batch;
uploading = false;
syncEvidenceSubmitButton(submitButton, uploadedTypes, uploading);
console.log(JSON.stringify({{disabledDuringSecondUpload, submitDisabled: submitButton.disabled, events, uploaded: result.uploaded, succeeded: result.succeeded.map(item => item.file.name), failed: result.failed.length}}));
"""

    completed = subprocess.run(
        ["node", "--input-type=module", "--eval", scenario],
        check=True,
        capture_output=True,
        text=True,
    )
    outcome = json.loads(completed.stdout)

    assert outcome == {
        "disabledDuringSecondUpload": True,
        "submitDisabled": False,
        "events": [
            "upload:first.png",
            "saved:first.png",
            "upload:second.png",
        ],
        "uploaded": 1,
        "succeeded": ["first.png"],
        "failed": 1,
    }

    evidence = source[source.index("function evidence") : source.index("\nasync function businessReport")]
    upload_callback = evidence[
        evidence.index("const result=await uploadEvidenceBatch") : evidence.index(
            "if(result.failed.length>0)"
        )
    ]
    assert "uploadedTypes.add(type)" in upload_callback
    assert "processing=false" in evidence
    assert "syncEvidenceSubmitButton(submitButton,uploadedTypes,processing" in evidence
    assert "submitButton.disabled=false" not in upload_callback
    assert "上传成功" in upload_callback
    assert 'id="evidence-file-results"' in evidence
    assert "renderEvidenceFileResults" in evidence
    assert "return {uploaded,succeeded,failed,results}" in source
