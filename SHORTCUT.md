# Optional macOS Shortcut

This gives you a GUI launcher while leaving the migration logic in the reviewed files.

## Fastest option

Double-click `Prepare Migration.command`. It runs diagnostics, inspects Things, creates a read-only snapshot and safe plan, and opens the run directory. It never writes to Reminders.

## One-time setup

1. Move the extracted folder somewhere permanent, for example:

   ```text
   ~/Developer/things-reminders-safe
   ```

2. Run once in Terminal:

   ```sh
   ~/Developer/things-reminders-safe/things-reminders doctor
   ```

3. Open **Shortcuts** on Mac and create a shortcut named **Things → Reminders Migration**.

## Actions

### 1. Choose from Menu

Create these menu entries:

- Inspect
- Plan
- Preflight
- Apply
- Verify
- Rollback Preview
- Rollback

### Inspect

Add **Run Shell Script**:

```sh
"$HOME/Developer/things-reminders-safe/things-reminders" inspect
```

Set input to **stdin**, then add **Show Result** using the shell output.

### Plan

Add **Choose from Menu** with:

- Safe plan, including recurring-project summaries
- Leave recurring projects for manual transfer
- Convert after-completion to fixed schedule

For **Safe plan**, use **Run Shell Script**:

```sh
"$HOME/Developer/things-reminders-safe/things-reminders" plan --unsupported=manual
```

For **Leave recurring projects for manual transfer**:

```sh
"$HOME/Developer/things-reminders-safe/things-reminders" plan --unsupported=manual --recurring-projects=manual
```

For the conversion option:

```sh
"$HOME/Developer/things-reminders-safe/things-reminders" plan --unsupported=manual --after-completion=fixed
```

Add **Show Result**. Copy the printed run-directory path.

Monthly rules such as “every 1st” or “every 21st” transfer automatically. “Last day of every month” is intentionally placed in `MANUAL_REVIEW.md`, because Reminders does not preserve that EventKit rule reliably on the tested macOS build.

### Preflight / Verify

1. Add **Ask for Input**:
   - Prompt: `Run directory`
   - Type: Text
2. Add **Run Shell Script**, passing the input **as arguments**:

Preflight:

```sh
"$HOME/Developer/things-reminders-safe/things-reminders" preflight "$1"
```

Verify:

```sh
"$HOME/Developer/things-reminders-safe/things-reminders" verify "$1"
```

3. Add **Show Result**.

### Apply

1. **Ask for Input**: `Run directory`.
2. **Get File**: select `report.txt` from that directory, or open it manually first.
3. Add **Show Alert**:
   - Title: `Create the planned Reminders?`
   - Message: `Only continue after reviewing report.txt and plan.csv. Existing reminders will not be modified.`
   - Enable Cancel.
4. **Run Shell Script**, input as arguments:

```sh
"$HOME/Developer/things-reminders-safe/things-reminders" apply "$1" --yes
```

5. **Show Result**.

`--yes` is appropriate here only because the preceding Shortcuts alert provides the explicit confirmation. In Terminal, omit it to require typing the run ID.

### Rollback Preview

1. **Ask for Input**: `Run directory`.
2. **Run Shell Script**, input as arguments:

```sh
"$HOME/Developer/things-reminders-safe/things-reminders" rollback-preview "$1"
```

3. Add **Show Result**. Nothing is deleted.

### Rollback

1. **Ask for Input**: `Run directory`.
2. Add **Show Alert**:
   - Title: `Rollback this migration?`
   - Message: `The tool previews first. It deletes only exact manifest/marker-matched imported reminders. Marker mismatches and non-empty lists are retained for manual review.`
   - Enable Cancel.
3. **Run Shell Script**, input as arguments:

```sh
"$HOME/Developer/things-reminders-safe/things-reminders" rollback "$1" --yes
```

4. **Show Result**.

## Permissions

The first EventKit preflight may trigger a macOS Reminders permission prompt. Grant full access to **Things Reminders Migrator**. If the Plan action cannot open the Things database, grant Full Disk Access to **Shortcuts**, or create the plan from Terminal/`Prepare Migration.command` instead.
