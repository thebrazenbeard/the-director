# v0.7 host evidence: UNQUALIFIED_HOST / NO_GO

This is source-only readiness guidance, not installation permission. The v0.6
`assess_unattended_readiness()` contract is unchanged: `qualified=False`.
The v0.7 inventory assessor likewise **always** returns `UNQUALIFIED_HOST`,
`qualified=False`, `NO_GO`, `NOT_GRANTED`, and `NOT_EXECUTED`. The latter means
no Director effects were executed; the inventory script does perform reads.
Windows lacks an implemented native credential-bound broker in this milestone;
that is a hard NO-GO, even if every synthetic broker test passes elsewhere.

## Evidence categories

| Category | What it establishes | What it does not establish |
| --- | --- | --- |
| DOC | Required checks and falsifiers | Successful execution of any check |
| SOURCE | Fail-closed parser/renderer and opt-in inventory script | Host isolation or service readiness |
| TEST | Synthetic malformed/forged/replayed input rejection | Windows OS enforcement or uptime |
| REAL_HOST_PROBE | An actually observed read-only inventory, when independently collected | Broker/worker separation, custody, crash safety |
| UNPROVEN | Missing independent proof; every security check is NOT_TESTED | Permission to assume PASS |

The parser's `REAL_HOST_PROBE` field is an **untrusted provenance claim**, not
authentication. It is rendered as `UNVERIFIED_SNAPSHOT`, never verified host
evidence. A copied, stale, wrong-host, wrong-boot or wrong-commit inventory can
look perfectly valid: none is usable as authorization. Unknown fields including
PASS booleans, signatures, host/boot/commit claims and approval flags are rejected.
No key, signature-verifier or qualified branch exists in this API. Even a future
genuine signed report needs a separate reviewed collection/verification design.

Independent proof would require an identified collection principal outside
worker control, tamper-resistant evidence custody, binding to the actual host,
boot, binary, configuration and source commit, a trustworthy freshness policy,
and independent verification. This source milestone establishes **none** of
those. Keep identifying host records in protected operator custody, not GitHub.

## Safe inventory and bounds

From a future **approved local checkout**, run these exact read-only commands:

```powershell
python -B -m unittest discover -s tests -p test_director_v07_lappy_qualification.py -v
powershell.exe -NoProfile -NonInteractive -File .\scripts\director-v07-qualify-lappy.ps1
```

Do not bypass execution policy if blocked. No elevation is required or requested.
This is an inventory test, **not a hostile-worker qualification command**. There
is intentionally no service-installation or native isolation-test command: that
implementation is absent. Future approved host qualification needs a reviewed
native test harness before any account, ACL, task or process-tree changes.

The script reads only in-process Windows version/build, logical processor count,
PowerShell major version and process bitness. OS version reporting may depend on
the runtime's compatibility behavior; it is not an attested Windows edition.
It performs no subprocess, WMI/CIM, task, service, credential, network or private
process inspection, and writes only stdout/stderr. It emits no usernames, SIDs,
hostnames, filesystem paths, full task arguments, command lines or unique IDs.
It uses a two-second cooperative elapsed check and a 4096-byte UTF-8 output cap;
these bound accepted output, not stalled PowerShell startup or an OS syscall.
The Windows test runner additionally enforces a ten-second child timeout. No
hard real-time host guarantee follows. Python accepts at most 4096 input bytes,
rejects duplicate keys/nonfinite/deep malformed JSON and never echoes errors.
Importing the Python module performs no inventory or host action.

## Required independent host falsifiers (all NOT_TESTED)

| Boundary | Future independently supervised falsifier |
| --- | --- |
| Distinct SIDs | Show broker and worker token SIDs differ; same identity fails |
| Broker key/DB ACL | Worker read/write/open of protected broker keys and SQLite fails |
| Named-pipe identity | Forged worker label or unauthorized connecting token is rejected |
| Job/tree cleanup | Escaping descendants or surviving resource-limit termination fails |
| Restart/resource caps | Repeated faults cannot exceed configured restart/time/memory limits |
| Stop/pause | Requests fence future admission; no claim that prior reads are undone |
| One-instance lock | Simultaneous independent starts cannot create two owners |
| Sleep/reboot | Resume/reboot cannot silently authorize work or replay prior effects |
| Receipt crash matrix | Interrupt before/after commit and before ACK; no duplicate admission/effect claim |
| Revocation crash matrix | Interrupt before/after epoch commit, admission and claim; stale capabilities fail |
| Evidence custody | Worker edits, stale snapshots, wrong boot/host/binary/config/commit cannot verify |

These tests involve real OS principals and controlled crash/restart experiments
and require separate operator approval and a safe harness; this document does
not perform or authorize them. Existing scheduled tasks must remain untouched.
Source tests and safe inventory alone cannot justify unattended installation.

Prior separately observed, read-only Lappy discovery established only basic
Windows/hardware/Python availability and an existing scheduled task; no project
checkout was present at the expected location. Identifying account/path/task
records are intentionally omitted. That discovery did not test principal
separation, native IPC, protected storage, restart behavior or service readiness.

## Known source-only clock limitation

Broker delivery leases persist `time.time()` wall-clock deadlines. A backward
host-clock adjustment can extend their effective lifetime; no clock rollback,
sleep/reboot, or trusted-clock qualification is claimed. Epoch revocation and
delivery-token fencing remain separate controls, not a clock guarantee. Host
qualification must resolve this risk before relying on lease duration.
