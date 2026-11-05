# Hostile demo: what the system can actually prove

Use synthetic fixtures openly. Do not call a residential address a shell company or a former researcher a fraudster. Do not imply that a team draft was delivered to an external author.

## 1. Corporate completion and crash recovery

Use a fresh data directory so the action has not already completed:

```powershell
$env:CORE_CRASH_AT = 'after_effect'
.\.venv\Scripts\python.exe -m diligence_core.cli --data-dir runtime/crash-demo --demo-trust run diligence_core/fixtures/corporate_invoice.txt
# Process exits 137 after the sandbox commits, before the workflow acknowledges it.
Remove-Item Env:\CORE_CRASH_AT
.\.venv\Scripts\python.exe -m diligence_core.cli --data-dir runtime/crash-demo --demo-trust effects
.\.venv\Scripts\python.exe -m diligence_core.cli --data-dir runtime/crash-demo --demo-trust resume
.\.venv\Scripts\python.exe -m diligence_core.cli --data-dir runtime/crash-demo --demo-trust effects
```

Show one effect before and after restart, the same effect reference, and the completed case. Crashing before the payment is insufficient evidence; this checkpoint is after the sink's committed effect.

## 2. Academic diligence and team handoff

```powershell
.\.venv\Scripts\python.exe -m diligence_core.cli --data-dir runtime/research-demo --demo-trust run diligence_core/fixtures/academic_address_discrepancy.txt
.\.venv\Scripts\python.exe -m diligence_core.cli --data-dir runtime/research-demo --demo-trust effects
```

Show that the supplier and author institution are different canonical records. The author profile lists a past affiliation and a candidate current institution. The claimed supplier address geocodes far from the trusted reference; the evidence shows both coordinate pairs and WGS84 distance. The outcome is a **team review draft**, not a fraud verdict or an external email.

Reupload the same document and show one draft. The automated tests also crash this draft path after sink commit and prove recovery without duplication.

## 3. Adversarial controls

Show test results for a changed currency/IFSC/amount, two invoice numbers sharing one authorization, an ambiguous person match, empty search results and approximate coordinates. Unknown facts stop invoice payment; approximate coordinates never become a precise fraud finding. A changed payload under an existing key is a conflict, not a new action.

Suggested narration: "We share identity, evidence and recovery infrastructure across corporate and academic documents. Each service has an explicit final action and its own authority checks. We can show what happened once, what the sources actually say, and what remains unknown."
