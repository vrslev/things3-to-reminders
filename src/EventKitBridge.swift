import Foundation
import EventKit
import CryptoKit

// macOS-only EventKit bridge. All source planning happens separately and read-only.

struct Plan: Codable {
    let schema: String
    let runID: String
    let createdAt: String
    let databaseVersion: Int
    let afterCompletionPolicy: String
    let unsupportedPolicy: String?
    let recurringProjectPolicy: String?
    let listPrefix: String
    let calendarTitles: [String]
    let items: [PlanItem]
    let unsupported: [UnsupportedItem]
    let warnings: [String]
    let blocked: Bool
}

struct UnsupportedItem: Codable {
    let sourceID: String
    let title: String
    let reason: String
}

struct PlanItem: Codable {
    let sourceID: String
    let sourceInstanceID: String
    let title: String
    let notes: String
    let url: String?
    let listTitle: String
    let marker: String?
    let dueDate: String?
    let alarmDate: String?
    let priority: Int
    let recurrenceRules: [RecurrenceSpec]
    let recurrenceOriginalMode: String?
    let warnings: [String]
}

struct RecurrenceSpec: Codable {
    let frequency: String
    let interval: Int
    let weekdays: [Int]
    let daysOfMonth: [Int]
    let monthsOfYear: [Int]
    let setPositions: [Int]?
    let endDate: String?
    let occurrenceCount: Int?
}

struct Manifest: Codable {
    var schema: String = "things-reminders-manifest/v3"
    var runID: String
    var planSHA256: String
    var status: String
    var createdAt: String
    var updatedAt: String
    var calendars: [CalendarRecord]
    var reminders: [ReminderRecord]
    var errors: [String]
    var rollback: RollbackRecord?
    var sourceIdentifier: String? = nil
    var sourceTitle: String? = nil
}

struct CalendarRecord: Codable, Equatable {
    let title: String
    let identifier: String
    let createdByMigration: Bool
}

struct ReminderRecord: Codable, Equatable {
    let sourceID: String
    let sourceInstanceID: String
    let marker: String?
    let title: String
    let reminderIdentifier: String
    let calendarIdentifier: String
    let createdAt: String
    let recoveredAfterInterruptedWrite: Bool
}

struct RollbackRecord: Codable {
    let startedAt: String
    let finishedAt: String
    let deletedReminderIDs: [String]
    let missingReminderIDs: [String]
    let protectedMismatchIDs: [String]
    let remainingMarkerIDs: [String]
    let deletedCalendarIDs: [String]
    let retainedCalendarIDs: [String]
}

struct StateFile: Codable {
    let schema: String
    let runID: String
    let status: String
    let updatedAt: String
}

enum BridgeError: Error, CustomStringConvertible {
    case usage(String)
    case invalid(String)
    case access(String)
    case eventKit(String)

    var description: String {
        switch self {
        case .usage(let value), .invalid(let value), .access(let value), .eventKit(let value): return value
        }
    }
}

let encoder: JSONEncoder = {
    let value = JSONEncoder()
    value.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
    return value
}()
let decoder = JSONDecoder()
let isoFormatter: ISO8601DateFormatter = {
    let f = ISO8601DateFormatter()
    f.formatOptions = [.withInternetDateTime]
    return f
}()

func utcNow() -> String { isoFormatter.string(from: Date()) }

func atomicWrite<T: Encodable>(_ value: T, to url: URL) throws {
    let data = try encoder.encode(value)
    let tmp = url.deletingLastPathComponent().appendingPathComponent(".\(url.lastPathComponent).\(UUID().uuidString)")
    try data.write(to: tmp, options: [.atomic])
    if FileManager.default.fileExists(atPath: url.path) {
        _ = try FileManager.default.replaceItemAt(url, withItemAt: tmp)
    } else {
        try FileManager.default.moveItem(at: tmp, to: url)
    }
}

func readJSON<T: Decodable>(_ type: T.Type, from url: URL) throws -> T {
    do { return try decoder.decode(type, from: Data(contentsOf: url)) }
    catch { throw BridgeError.invalid("Cannot read \(url.path): \(error)") }
}

func fileSHA256(_ url: URL) throws -> String {
    let digest = SHA256.hash(data: try Data(contentsOf: url))
    return digest.map { String(format: "%02x", $0) }.joined()
}

func metadataValue(_ notes: String?, key: String) -> String? {
    guard let notes else { return nil }
    let prefix = "\(key): "
    let values = notes.split(separator: "\n", omittingEmptySubsequences: false)
        .map { String($0).trimmingCharacters(in: .whitespacesAndNewlines) }
        .filter { $0.hasPrefix(prefix) }
        .map { String($0.dropFirst(prefix.count)).trimmingCharacters(in: .whitespaces) }
    guard values.count == 1, let value = values.first, !value.isEmpty else { return nil }
    return value
}

func hasMetadata(_ notes: String?, key: String, value: String) -> Bool {
    metadataValue(notes, key: key) == value
}

func reminderRecordsBySource(_ manifest: Manifest) throws -> [String: ReminderRecord] {
    var result: [String: ReminderRecord] = [:]
    var identifiers = Set<String>()
    for record in manifest.reminders {
        guard result[record.sourceID] == nil else {
            throw BridgeError.invalid("Manifest contains duplicate source ID: \(record.sourceID)")
        }
        guard identifiers.insert(record.reminderIdentifier).inserted else {
            throw BridgeError.invalid("Manifest contains duplicate reminder ID: \(record.reminderIdentifier)")
        }
        result[record.sourceID] = record
    }
    return result
}

func writeRollbackInventory(_ manifest: Manifest, nextTo manifestURL: URL) throws {
    var lines: [String] = [
        "THINGS → REMINDERS ROLLBACK INVENTORY",
        "Run ID: \(manifest.runID)",
        "Status: \(manifest.status)",
        "Plan SHA-256: \(manifest.planSHA256)",
        "",
        "LISTS",
    ]
    for record in manifest.calendars.sorted(by: { $0.title.localizedCaseInsensitiveCompare($1.title) == .orderedAscending }) {
        lines.append("- \(record.title)")
        lines.append("  EventKit-ID: \(record.identifier)")
    }
    lines += ["", "REMINDERS"]
    for record in manifest.reminders.sorted(by: { $0.title.localizedCaseInsensitiveCompare($1.title) == .orderedAscending }) {
        lines.append("- \(record.title)")
        lines.append("  EventKit-ID: \(record.reminderIdentifier)")
        lines.append("  List-ID: \(record.calendarIdentifier)")
        lines.append("  Open: x-apple-reminderkit://REMCDReminder/\(record.reminderIdentifier)")
    }
    lines += [
        "",
        "MANUAL REVERSAL",
        "1. Prefer: ./things-reminders rollback-preview RUN_DIR",
        "2. Then:   ./things-reminders rollback RUN_DIR",
        "3. The EventKit IDs above are the authoritative rollback inventory.",
        "4. Without the tool, delete the isolated lists above only after checking they contain no post-migration user items.",
        "5. For a reminder moved elsewhere, use its exact Open URL above and delete that reminder manually.",
        "",
    ]
    let data = lines.joined(separator: "\n").data(using: .utf8)!
    let url = manifestURL.deletingLastPathComponent().appendingPathComponent("ROLLBACK_INVENTORY.txt")
    let tmp = url.deletingLastPathComponent().appendingPathComponent(".ROLLBACK_INVENTORY.\(UUID().uuidString)")
    try data.write(to: tmp, options: [.atomic])
    if FileManager.default.fileExists(atPath: url.path) {
        _ = try FileManager.default.replaceItemAt(url, withItemAt: tmp)
    } else {
        try FileManager.default.moveItem(at: tmp, to: url)
    }
}

func persistManifest(_ manifest: Manifest, to manifestURL: URL) throws {
    try atomicWrite(manifest, to: manifestURL)
    try writeRollbackInventory(manifest, nextTo: manifestURL)
}

func requestReminderAccess(_ store: EKEventStore) throws {
    let semaphore = DispatchSemaphore(value: 0)
    var granted = false
    var resultError: Error?
    if #available(macOS 14.0, *) {
        store.requestFullAccessToReminders { ok, error in
            granted = ok
            resultError = error
            semaphore.signal()
        }
    } else {
        store.requestAccess(to: .reminder) { ok, error in
            granted = ok
            resultError = error
            semaphore.signal()
        }
    }
    semaphore.wait()
    if let resultError { throw BridgeError.access("Reminders permission error: \(resultError)") }
    if !granted { throw BridgeError.access("Full access to Reminders was not granted") }
}

func parseLocalDate(_ value: String) throws -> Date {
    let parts = value.split(separator: "-").compactMap { Int($0) }
    guard parts.count == 3 else { throw BridgeError.invalid("Invalid date: \(value)") }
    var comps = DateComponents()
    comps.calendar = Calendar.current
    comps.timeZone = TimeZone.current
    comps.year = parts[0]
    comps.month = parts[1]
    comps.day = parts[2]
    comps.hour = 12 // avoids DST boundary ambiguity for recurrence ends
    guard let date = Calendar.current.date(from: comps) else { throw BridgeError.invalid("Invalid date: \(value)") }
    return date
}

func localStartOfDay(_ value: String) throws -> Date {
    let date = try parseLocalDate(value)
    return Calendar.current.startOfDay(for: date)
}

func parseLocalDateTime(_ value: String) throws -> Date {
    // Planner emits YYYY-MM-DDTHH:mm with intentionally local semantics.
    let formatter = DateFormatter()
    formatter.locale = Locale(identifier: "en_US_POSIX")
    formatter.calendar = Calendar(identifier: .gregorian)
    formatter.timeZone = TimeZone.current
    formatter.dateFormat = "yyyy-MM-dd'T'HH:mm"
    guard let date = formatter.date(from: value) else { throw BridgeError.invalid("Invalid local date-time: \(value)") }
    return date
}

func dueComponents(_ value: String) throws -> DateComponents {
    let date = try parseLocalDate(value)
    var comps = Calendar.current.dateComponents([.year, .month, .day], from: date)
    comps.calendar = Calendar.current
    comps.timeZone = TimeZone.current
    return comps
}

func recurrenceRule(from spec: RecurrenceSpec) throws -> EKRecurrenceRule {
    let frequency: EKRecurrenceFrequency
    switch spec.frequency {
    case "daily": frequency = .daily
    case "weekly": frequency = .weekly
    case "monthly": frequency = .monthly
    case "yearly": frequency = .yearly
    default: throw BridgeError.invalid("Unsupported EventKit frequency: \(spec.frequency)")
    }
    guard spec.interval > 0 else { throw BridgeError.invalid("Recurrence interval must be positive") }

    let daysOfWeek: [EKRecurrenceDayOfWeek]? = spec.weekdays.isEmpty ? nil : try spec.weekdays.map { raw in
        guard let weekday = EKWeekday(rawValue: raw) else { throw BridgeError.invalid("Invalid EventKit weekday: \(raw)") }
        return EKRecurrenceDayOfWeek(weekday)
    }
    let daysOfMonth: [NSNumber]? = spec.daysOfMonth.isEmpty ? nil : spec.daysOfMonth.map { NSNumber(value: $0) }
    let monthsOfYear: [NSNumber]? = spec.monthsOfYear.isEmpty ? nil : spec.monthsOfYear.map { NSNumber(value: $0) }
    let positions = spec.setPositions ?? []
    let setPositions: [NSNumber]? = positions.isEmpty ? nil : positions.map { NSNumber(value: $0) }

    let recurrenceEnd: EKRecurrenceEnd?
    if let count = spec.occurrenceCount, count > 0 {
        recurrenceEnd = EKRecurrenceEnd(occurrenceCount: count)
    } else if let value = spec.endDate {
        recurrenceEnd = EKRecurrenceEnd(end: try parseLocalDate(value))
    } else {
        recurrenceEnd = nil
    }

    return EKRecurrenceRule(
        recurrenceWith: frequency,
        interval: spec.interval,
        daysOfTheWeek: daysOfWeek,
        daysOfTheMonth: daysOfMonth,
        monthsOfTheYear: monthsOfYear,
        weeksOfTheYear: nil,
        daysOfTheYear: nil,
        setPositions: setPositions,
        end: recurrenceEnd
    )
}

func fetchReminders(_ store: EKEventStore, calendars: [EKCalendar]) throws -> [EKReminder] {
    if calendars.isEmpty { return [] }
    let predicate = store.predicateForReminders(in: calendars)
    let semaphore = DispatchSemaphore(value: 0)
    var result: [EKReminder] = []
    store.fetchReminders(matching: predicate) { reminders in
        result = reminders ?? []
        semaphore.signal()
    }
    semaphore.wait()
    return result
}

func calendarByIdentifier(_ store: EKEventStore, _ identifier: String) -> EKCalendar? {
    store.calendar(withIdentifier: identifier)
}

func reminderByIdentifier(_ store: EKEventStore, _ identifier: String) -> EKReminder? {
    store.calendarItem(withIdentifier: identifier) as? EKReminder
}

func sourceByIdentifier(_ store: EKEventStore, _ identifier: String) -> EKSource? {
    store.sources.first { $0.sourceIdentifier == identifier }
}

func chooseWritableSource(_ store: EKEventStore) throws -> EKSource {
    if let source = store.defaultCalendarForNewReminders()?.source { return source }
    if let source = store.sources.first(where: { source in
        source.sourceType == .calDAV || source.sourceType == .local || source.sourceType == .exchange
    }) { return source }
    throw BridgeError.eventKit("No writable Reminders account/source was found")
}

func ensureCalendars(plan: Plan, store: EKEventStore, source: EKSource, manifest: inout Manifest, manifestURL: URL) throws -> [String: EKCalendar] {
    var result: [String: EKCalendar] = [:]
    let current = store.calendars(for: .reminder)

    for title in plan.calendarTitles {
        if let record = manifest.calendars.first(where: { $0.title == title }),
           let calendar = calendarByIdentifier(store, record.identifier) {
            guard calendar.title == title else {
                throw BridgeError.eventKit("Migration list was renamed after creation: \(record.title)")
            }
            guard calendar.source.sourceIdentifier == source.sourceIdentifier else {
                throw BridgeError.eventKit("Migration list moved to another Reminders account: \(title)")
            }
            result[title] = calendar
            continue
        }
        let titleMatches = current.filter { $0.title == title }
        if titleMatches.count > 1 {
            throw BridgeError.eventKit("Multiple destination lists have the same migration title: \(title)")
        }
        if let existing = titleMatches.first {
            // The default list title contains the run ID. This lets us recover a list
            // created immediately before an interrupted manifest write without adding
            // hidden metadata to reminder notes.
            guard title.contains(plan.runID) else {
                throw BridgeError.eventKit("Ambiguous destination-list collision: \(title). Delete/rename that list, then retry.")
            }
            guard existing.source.sourceIdentifier == source.sourceIdentifier else {
                throw BridgeError.eventKit("Recovered migration list is in a different Reminders account: \(title)")
            }
            let planned = plan.items.filter { $0.listTitle == title }
            let contents = try fetchReminders(store, calendars: [existing])
            let unrelated = contents.filter { reminder in
                !planned.contains { validationIssues(item: $0, reminder: reminder).isEmpty }
            }
            guard unrelated.isEmpty else {
                throw BridgeError.eventKit("Destination-list collision contains reminders not matching this plan: \(title)")
            }
            let record = CalendarRecord(title: title, identifier: existing.calendarIdentifier, createdByMigration: true)
            if !manifest.calendars.contains(record) { manifest.calendars.append(record) }
            try persistManifest(manifest, to: manifestURL)
            result[title] = existing
            continue
        }
        let calendar = EKCalendar(for: .reminder, eventStore: store)
        calendar.title = title
        calendar.source = source
        do { try store.saveCalendar(calendar, commit: true) }
        catch { throw BridgeError.eventKit("Cannot create Reminders list '\(title)': \(error)") }
        let record = CalendarRecord(title: title, identifier: calendar.calendarIdentifier, createdByMigration: true)
        manifest.calendars.append(record)
        manifest.updatedAt = utcNow()
        try persistManifest(manifest, to: manifestURL)
        result[title] = calendar
    }
    return result
}

func recurrenceFrequencyName(_ value: EKRecurrenceFrequency) -> String {
    switch value {
    case .daily: return "daily"
    case .weekly: return "weekly"
    case .monthly: return "monthly"
    case .yearly: return "yearly"
    @unknown default: return "unknown"
    }
}

func dateOnlyString(_ components: DateComponents?) -> String? {
    guard let components, let year = components.year, let month = components.month, let day = components.day else { return nil }
    return String(format: "%04d-%02d-%02d", year, month, day)
}

func validationIssues(item: PlanItem, reminder: EKReminder) -> [String] {
    var issues: [String] = []
    if reminder.title != item.title { issues.append("title mismatch") }
    if reminder.calendar.title != item.listTitle { issues.append("list mismatch") }
    let notes = reminder.notes ?? ""
    if notes != item.notes { issues.append("notes mismatch") }
    if reminder.priority != item.priority { issues.append("priority mismatch") }
    if dateOnlyString(reminder.dueDateComponents) != item.dueDate { issues.append("due date mismatch") }
    let expectedURL = item.url.flatMap { URL(string: $0)?.absoluteString }
    if reminder.url?.absoluteString != expectedURL { issues.append("URL mismatch") }

    let actualRules = reminder.recurrenceRules ?? []
    if actualRules.count != item.recurrenceRules.count {
        issues.append("recurrence rule count mismatch")
    } else {
        for (expected, actual) in zip(item.recurrenceRules, actualRules) {
            if recurrenceFrequencyName(actual.frequency) != expected.frequency { issues.append("recurrence frequency mismatch") }
            if actual.interval != expected.interval { issues.append("recurrence interval mismatch") }
            let weekdays = (actual.daysOfTheWeek ?? []).map { $0.dayOfTheWeek.rawValue }.sorted()
            if weekdays != expected.weekdays.sorted() { issues.append("recurrence weekdays mismatch") }
            let monthDays = (actual.daysOfTheMonth ?? []).map { $0.intValue }.sorted()
            if monthDays != expected.daysOfMonth.sorted() { issues.append("recurrence month-days mismatch") }
            let months = (actual.monthsOfTheYear ?? []).map { $0.intValue }.sorted()
            if months != expected.monthsOfYear.sorted() { issues.append("recurrence months mismatch") }
            let positions = (actual.setPositions ?? []).map { $0.intValue }.sorted()
            if positions != (expected.setPositions ?? []).sorted() { issues.append("recurrence set-positions mismatch") }
            if let count = expected.occurrenceCount {
                if actual.recurrenceEnd?.occurrenceCount != count { issues.append("recurrence count mismatch") }
            } else if let endDate = expected.endDate {
                let actualEnd = actual.recurrenceEnd?.endDate.map {
                    let comps = Calendar.current.dateComponents([.year, .month, .day], from: $0)
                    return String(format: "%04d-%02d-%02d", comps.year ?? 0, comps.month ?? 0, comps.day ?? 0)
                }
                if actualEnd != endDate { issues.append("recurrence end-date mismatch") }
            } else if actual.recurrenceEnd != nil {
                issues.append("unexpected recurrence end")
            }
        }
    }

    if let expectedAlarm = item.alarmDate, let dueValue = item.dueDate,
       let expectedDate = try? parseLocalDateTime(expectedAlarm),
       let dueStart = try? localStartOfDay(dueValue) {
        let expectedOffset = expectedDate.timeIntervalSince(dueStart)
        let hasMatchingAlarm = (reminder.alarms ?? []).contains { abs($0.relativeOffset - expectedOffset) < 60 }
        if !hasMatchingAlarm { issues.append("time alarm mismatch") }
    } else if item.alarmDate == nil {
        let unexpected = (reminder.alarms ?? []).contains { $0.structuredLocation == nil }
        if unexpected { issues.append("unexpected time alarm") }
    }
    return Array(Set(issues)).sorted()
}

func writeState(runURL: URL, runID: String, status: String) throws {
    let state = StateFile(schema: "things-reminders-state/v1", runID: runID, status: status, updatedAt: utcNow())
    try atomicWrite(state, to: runURL.appendingPathComponent("state.json"))
}

func apply(runURL: URL) throws {
    let planURL = runURL.appendingPathComponent("plan.json")
    let manifestURL = runURL.appendingPathComponent("manifest.json")
    let planHash = try fileSHA256(planURL)
    let plan = try readJSON(Plan.self, from: planURL)
    guard ["things-reminders-plan/v1", "things-reminders-plan/v2", "things-reminders-plan/v3", "things-reminders-plan/v4"].contains(plan.schema) else {
        throw BridgeError.invalid("Unknown plan schema: \(plan.schema)")
    }
    guard !plan.blocked else { throw BridgeError.invalid("Plan is blocked; resolve unsupported items before apply") }
    if !plan.unsupported.isEmpty && plan.unsupportedPolicy != "manual" {
        throw BridgeError.invalid("Plan contains unsupported items without manual-exclusion policy")
    }
    let setPositionItems = plan.items.filter { item in
        item.recurrenceRules.contains { !($0.setPositions ?? []).isEmpty }
    }
    if !setPositionItems.isEmpty {
        let titles = setPositionItems.prefix(5).map { $0.title }.joined(separator: ", ")
        throw BridgeError.invalid(
            "Plan contains BYSETPOS recurrence rules that Apple Reminders may ignore for reminders (for example: \(titles)). " +
            "Regenerate the plan with migrator v0.3.7 or newer."
        )
    }

    var manifest: Manifest
    if FileManager.default.fileExists(atPath: manifestURL.path) {
        manifest = try readJSON(Manifest.self, from: manifestURL)
        guard ["things-reminders-manifest/v2", "things-reminders-manifest/v3"].contains(manifest.schema) else {
            throw BridgeError.invalid("Unknown manifest schema: \(manifest.schema)")
        }
        guard manifest.runID == plan.runID else { throw BridgeError.invalid("Manifest/plan run ID mismatch") }
        guard manifest.planSHA256 == planHash else { throw BridgeError.invalid("plan.json changed after the manifest was created; refusing to resume") }
        _ = try reminderRecordsBySource(manifest)
        if manifest.status.hasPrefix("rollback") || manifest.status == "rolled_back" {
            throw BridgeError.invalid("This run entered rollback state. Create a new plan to apply again safely.")
        }
    } else {
        manifest = Manifest(
            runID: plan.runID,
            planSHA256: planHash,
            status: "applying",
            createdAt: utcNow(),
            updatedAt: utcNow(),
            calendars: [], reminders: [], errors: [], rollback: nil
        )
        try persistManifest(manifest, to: manifestURL)
    }

    let store = EKEventStore()
    try requestReminderAccess(store)
    let destinationSource: EKSource
    if let lockedIdentifier = manifest.sourceIdentifier {
        guard let locked = sourceByIdentifier(store, lockedIdentifier) else {
            throw BridgeError.eventKit("The Reminders account used by this run is no longer available")
        }
        destinationSource = locked
    } else {
        destinationSource = try chooseWritableSource(store)
    }
    manifest.sourceIdentifier = destinationSource.sourceIdentifier
    manifest.sourceTitle = destinationSource.title
    manifest.status = "applying"
    manifest.updatedAt = utcNow()
    try persistManifest(manifest, to: manifestURL)
    try writeState(runURL: runURL, runID: plan.runID, status: "applying")

    do {
        let calendars = try ensureCalendars(plan: plan, store: store, source: destinationSource, manifest: &manifest, manifestURL: manifestURL)
        var existing = try fetchReminders(store, calendars: Array(calendars.values))
        var usedReminderIDs = Set(manifest.reminders.map { $0.reminderIdentifier })

        for (index, item) in plan.items.enumerated() {
            if let record = manifest.reminders.first(where: { $0.sourceID == item.sourceID }),
               let existingReminder = reminderByIdentifier(store, record.reminderIdentifier) {
                let issues = validationIssues(item: item, reminder: existingReminder)
                guard issues.isEmpty else {
                    throw BridgeError.eventKit("Recorded reminder changed before resume: \(item.title): \(issues.joined(separator: ", "))")
                }
                print("[\(index + 1)/\(plan.items.count)] exists: \(item.title)")
                continue
            }
            guard let calendar = calendars[item.listTitle] else { throw BridgeError.eventKit("Missing destination list: \(item.listTitle)") }

            let recoverable = existing
                .filter { $0.calendar.calendarIdentifier == calendar.calendarIdentifier }
                .filter { !usedReminderIDs.contains($0.calendarItemIdentifier) }
                .filter { validationIssues(item: item, reminder: $0).isEmpty }
                .sorted { $0.calendarItemIdentifier < $1.calendarItemIdentifier }
            if let recovered = recoverable.first {
                let record = ReminderRecord(
                    sourceID: item.sourceID,
                    sourceInstanceID: item.sourceInstanceID,
                    marker: item.marker,
                    title: item.title,
                    reminderIdentifier: recovered.calendarItemIdentifier,
                    calendarIdentifier: recovered.calendar.calendarIdentifier,
                    createdAt: utcNow(),
                    recoveredAfterInterruptedWrite: true
                )
                manifest.reminders.removeAll { $0.sourceID == item.sourceID }
                manifest.reminders.append(record)
                usedReminderIDs.insert(recovered.calendarItemIdentifier)
                manifest.updatedAt = utcNow()
                try persistManifest(manifest, to: manifestURL)
                print("[\(index + 1)/\(plan.items.count)] recovered: \(item.title)")
                continue
            }

            let reminder = EKReminder(eventStore: store)
            reminder.calendar = calendar
            reminder.title = item.title
            reminder.notes = item.notes
            reminder.priority = item.priority
            if let value = item.url { reminder.url = URL(string: value) }
            if let value = item.dueDate { reminder.dueDateComponents = try dueComponents(value) }
            if let alarmValue = item.alarmDate, let dueValue = item.dueDate {
                let offset = try parseLocalDateTime(alarmValue).timeIntervalSince(localStartOfDay(dueValue))
                reminder.addAlarm(EKAlarm(relativeOffset: offset))
            }
            for rule in item.recurrenceRules { reminder.addRecurrenceRule(try recurrenceRule(from: rule)) }

            do { try store.save(reminder, commit: true) }
            catch { throw BridgeError.eventKit("Cannot create reminder '\(item.title)': \(error)") }
            guard reminderByIdentifier(store, reminder.calendarItemIdentifier) != nil else {
                throw BridgeError.eventKit("Reminder was saved but could not be read back: \(item.title)")
            }

            let record = ReminderRecord(
                sourceID: item.sourceID,
                sourceInstanceID: item.sourceInstanceID,
                marker: item.marker,
                title: item.title,
                reminderIdentifier: reminder.calendarItemIdentifier,
                calendarIdentifier: calendar.calendarIdentifier,
                createdAt: utcNow(),
                recoveredAfterInterruptedWrite: false
            )
            manifest.reminders.removeAll { $0.sourceID == item.sourceID }
            manifest.reminders.append(record)
            manifest.updatedAt = utcNow()
            try persistManifest(manifest, to: manifestURL)
            usedReminderIDs.insert(reminder.calendarItemIdentifier)
            existing.append(reminder)
            print("[\(index + 1)/\(plan.items.count)] created: \(item.title)")
        }

        let recordBySource = try reminderRecordsBySource(manifest)
        var postApplyIssues: [String] = []
        for item in plan.items {
            guard let record = recordBySource[item.sourceID], let reminder = reminderByIdentifier(store, record.reminderIdentifier) else {
                postApplyIssues.append("missing: \(item.title) [\(item.sourceID)]")
                continue
            }
            let issues = validationIssues(item: item, reminder: reminder)
            postApplyIssues.append(contentsOf: issues.map { "\(item.title) [\(item.sourceID)]: \($0)" })
        }
        guard manifest.reminders.count == plan.items.count, postApplyIssues.isEmpty else {
            throw BridgeError.eventKit("Post-apply verification failed:\n" + postApplyIssues.joined(separator: "\n"))
        }
        manifest.status = "applied"
        manifest.updatedAt = utcNow()
        try persistManifest(manifest, to: manifestURL)
        try writeState(runURL: runURL, runID: plan.runID, status: "applied")
        print("Applied and verified \(manifest.reminders.count) reminders in \(manifest.calendars.count) lists.")
    } catch {
        manifest.status = "partial"
        manifest.updatedAt = utcNow()
        manifest.errors.append("\(utcNow()): \(error)")
        try? persistManifest(manifest, to: manifestURL)
        try? writeState(runURL: runURL, runID: plan.runID, status: "partial")
        throw error
    }
}

func verify(runURL: URL) throws {
    let planURL = runURL.appendingPathComponent("plan.json")
    let plan = try readJSON(Plan.self, from: planURL)
    let manifest = try readJSON(Manifest.self, from: runURL.appendingPathComponent("manifest.json"))
    guard ["things-reminders-manifest/v2", "things-reminders-manifest/v3"].contains(manifest.schema) else {
        throw BridgeError.invalid("Unknown manifest schema: \(manifest.schema)")
    }
    guard plan.runID == manifest.runID else { throw BridgeError.invalid("Manifest/plan run ID mismatch") }
    guard manifest.planSHA256 == (try fileSHA256(planURL)) else { throw BridgeError.invalid("plan.json changed after apply") }
    let store = EKEventStore()
    try requestReminderAccess(store)
    let recordBySource = try reminderRecordsBySource(manifest)
    var present = 0
    var issueRows: [[String: Any]] = []
    var movedRows: [[String: String]] = []
    for item in plan.items {
        guard let record = recordBySource[item.sourceID], let reminder = reminderByIdentifier(store, record.reminderIdentifier) else {
            issueRows.append(["sourceID": item.sourceID, "title": item.title, "issues": ["missing reminder"]])
            continue
        }
        present += 1
        let issues = validationIssues(item: item, reminder: reminder)
        if !issues.isEmpty { issueRows.append(["sourceID": item.sourceID, "title": item.title, "issues": issues]) }
        if reminder.calendar.calendarIdentifier != record.calendarIdentifier {
            movedRows.append(["sourceID": item.sourceID, "title": item.title])
        }
    }
    let ok = issueRows.isEmpty && present == plan.items.count && manifest.reminders.count == plan.items.count
    let result: [String: Any] = [
        "runID": plan.runID,
        "manifestStatus": manifest.status,
        "planned": plan.items.count,
        "recorded": manifest.reminders.count,
        "present": present,
        "issues": issueRows,
        "movedAfterImport": movedRows,
        "ok": ok,
    ]
    let data = try JSONSerialization.data(withJSONObject: result, options: [.prettyPrinted, .sortedKeys])
    print(String(decoding: data, as: UTF8.self))
    if !ok { throw BridgeError.eventKit("Verification failed") }
}

func rollbackPreview(runURL: URL) throws {
    let manifest = try readJSON(Manifest.self, from: runURL.appendingPathComponent("manifest.json"))
    guard ["things-reminders-manifest/v2", "things-reminders-manifest/v3"].contains(manifest.schema) else {
        throw BridgeError.invalid("Unknown manifest schema: \(manifest.schema)")
    }
    _ = try reminderRecordsBySource(manifest)
    let store = EKEventStore()
    try requestReminderAccess(store)

    var presentIDs = 0
    var missingIDs = 0
    var legacyMarkerMismatches = 0
    let legacy = manifest.schema == "things-reminders-manifest/v2"
    for record in manifest.reminders {
        guard let reminder = reminderByIdentifier(store, record.reminderIdentifier) else {
            missingIDs += 1
            continue
        }
        presentIDs += 1
        if legacy {
            let markerOK = hasMetadata(reminder.notes, key: "Migration-ID", value: manifest.runID)
                && hasMetadata(reminder.notes, key: "Things-ID", value: record.sourceID)
            if !markerOK { legacyMarkerMismatches += 1 }
        }
    }

    var emptyLists = 0
    var retainedLists = 0
    for record in manifest.calendars where record.createdByMigration {
        guard let calendar = calendarByIdentifier(store, record.identifier) else { continue }
        if try fetchReminders(store, calendars: [calendar]).isEmpty { emptyLists += 1 }
        else { retainedLists += 1 }
    }

    let result: [String: Any] = [
        "runID": manifest.runID,
        "status": manifest.status,
        "recordedReminders": manifest.reminders.count,
        "presentRecordedIDs": presentIDs,
        "missingRecordedIDs": missingIDs,
        "legacyMarkerMismatches": legacyMarkerMismatches,
        "emptyMigrationLists": emptyLists,
        "nonEmptyListsThatWillBeRetainedUnlessEmptied": retainedLists,
        "rollbackIdentity": legacy ? "EventKit ID + legacy note markers" : "exact EventKit IDs from manifest.json",
    ]
    let data = try JSONSerialization.data(withJSONObject: result, options: [.prettyPrinted, .sortedKeys])
    print(String(decoding: data, as: UTF8.self))
}

func rollback(runURL: URL) throws {
    let manifestURL = runURL.appendingPathComponent("manifest.json")
    var manifest = try readJSON(Manifest.self, from: manifestURL)
    guard ["things-reminders-manifest/v2", "things-reminders-manifest/v3"].contains(manifest.schema) else {
        throw BridgeError.invalid("Unknown manifest schema: \(manifest.schema)")
    }
    _ = try reminderRecordsBySource(manifest)
    if manifest.status == "rolled_back" {
        print("Already rolled back.")
        return
    }
    let store = EKEventStore()
    try requestReminderAccess(store)
    let started = utcNow()
    var deletedIDs: [String] = []
    var missingIDs: [String] = []
    var protectedMismatchIDs: [String] = []
    let legacy = manifest.schema == "things-reminders-manifest/v2"

    // EventKit IDs captured immediately after creation are authoritative for v3.
    // Legacy v2 runs retain their old note-marker protection.
    for record in manifest.reminders {
        if let reminder = reminderByIdentifier(store, record.reminderIdentifier) {
            if legacy {
                let belongsToRun = hasMetadata(reminder.notes, key: "Migration-ID", value: manifest.runID)
                    && hasMetadata(reminder.notes, key: "Things-ID", value: record.sourceID)
                guard belongsToRun else {
                    protectedMismatchIDs.append(record.reminderIdentifier)
                    continue
                }
            }
            do {
                try store.remove(reminder, commit: true)
                deletedIDs.append(record.reminderIdentifier)
            } catch {
                throw BridgeError.eventKit("Cannot remove imported reminder '\(record.title)': \(error)")
            }
        } else {
            missingIDs.append(record.reminderIdentifier)
        }
    }

    var remainingMarkerIDs: [String] = []
    if legacy {
        // Legacy runs used note markers to recover save-before-manifest failures.
        let allCalendars = store.calendars(for: .reminder)
        let leftovers = try fetchReminders(store, calendars: allCalendars)
        for reminder in leftovers where hasMetadata(reminder.notes, key: "Migration-ID", value: manifest.runID) {
            try store.remove(reminder, commit: true)
            deletedIDs.append(reminder.calendarItemIdentifier)
        }
        let afterDeletion = try fetchReminders(store, calendars: store.calendars(for: .reminder))
        remainingMarkerIDs = afterDeletion
            .filter { hasMetadata($0.notes, key: "Migration-ID", value: manifest.runID) }
            .map { $0.calendarItemIdentifier }
    }

    var deletedCalendarIDs: [String] = []
    var retainedCalendarIDs: [String] = []
    for record in manifest.calendars where record.createdByMigration {
        guard let calendar = calendarByIdentifier(store, record.identifier) else { continue }
        let remaining = try fetchReminders(store, calendars: [calendar])
        if remaining.isEmpty {
            do {
                try store.removeCalendar(calendar, commit: true)
                deletedCalendarIDs.append(record.identifier)
            } catch {
                retainedCalendarIDs.append(record.identifier)
            }
        } else {
            // User content or an unrecorded item remains; preserve the list.
            retainedCalendarIDs.append(record.identifier)
        }
    }

    let incomplete = !protectedMismatchIDs.isEmpty || !remainingMarkerIDs.isEmpty
    manifest.status = incomplete ? "rollback_incomplete" : "rolled_back"
    manifest.updatedAt = utcNow()
    manifest.rollback = RollbackRecord(
        startedAt: started,
        finishedAt: utcNow(),
        deletedReminderIDs: Array(Set(deletedIDs)).sorted(),
        missingReminderIDs: Array(Set(missingIDs)).sorted(),
        protectedMismatchIDs: Array(Set(protectedMismatchIDs)).sorted(),
        remainingMarkerIDs: Array(Set(remainingMarkerIDs)).sorted(),
        deletedCalendarIDs: deletedCalendarIDs.sorted(),
        retainedCalendarIDs: retainedCalendarIDs.sorted()
    )
    try persistManifest(manifest, to: manifestURL)
    try writeState(runURL: runURL, runID: manifest.runID, status: manifest.status)
    print("Rollback deleted \(Set(deletedIDs).count) reminders and \(deletedCalendarIDs.count) empty lists.")
    if !retainedCalendarIDs.isEmpty { print("Retained \(retainedCalendarIDs.count) non-empty lists to protect user data.") }
    if incomplete {
        throw BridgeError.eventKit(
            "Legacy rollback stopped safely with marker mismatches. Inspect manifest.json and MANUAL_ROLLBACK.txt."
        )
    }
    print("Rollback complete.")
}

func preflight(runURL: URL) throws {
    let plan = try readJSON(Plan.self, from: runURL.appendingPathComponent("plan.json"))
    guard ["things-reminders-plan/v1", "things-reminders-plan/v2", "things-reminders-plan/v3", "things-reminders-plan/v4"].contains(plan.schema) else {
        throw BridgeError.invalid("Unknown plan schema: \(plan.schema)")
    }
    guard !plan.blocked else { throw BridgeError.invalid("Plan is blocked") }
    if !plan.unsupported.isEmpty && plan.unsupportedPolicy != "manual" {
        throw BridgeError.invalid("Plan contains unsupported items without manual-exclusion policy")
    }
    let setPositionItems = plan.items.filter { item in
        item.recurrenceRules.contains { !($0.setPositions ?? []).isEmpty }
    }
    if !setPositionItems.isEmpty {
        let titles = setPositionItems.prefix(5).map { $0.title }.joined(separator: ", ")
        throw BridgeError.invalid(
            "Plan contains BYSETPOS recurrence rules that Apple Reminders may ignore for reminders (for example: \(titles)). " +
            "Regenerate the plan with migrator v0.3.7 or newer."
        )
    }
    let store = EKEventStore()
    try requestReminderAccess(store)
    let destinationSource = try chooseWritableSource(store)
    print("Destination Reminders account: \(destinationSource.title) [\(destinationSource.sourceIdentifier)]")
    for item in plan.items {
        if item.dueDate == nil && !item.recurrenceRules.isEmpty {
            throw BridgeError.invalid("Recurring item lacks first due date: \(item.title)")
        }
        for rule in item.recurrenceRules { _ = try recurrenceRule(from: rule) }
    }
    print("Preflight OK: \(plan.items.count) reminders, \(plan.calendarTitles.count) lists, \(plan.items.filter { !$0.recurrenceRules.isEmpty }.count) recurring.")
}

func usage() -> Never {
    fputs("Usage: EventKitBridge preflight|apply|verify|rollback-preview|rollback <run-directory>\n", stderr)
    exit(64)
}

func main() throws {
    let args = CommandLine.arguments
    guard args.count == 3 else { usage() }
    let command = args[1]
    let runURL = URL(fileURLWithPath: NSString(string: args[2]).expandingTildeInPath, isDirectory: true)
    guard FileManager.default.fileExists(atPath: runURL.path) else { throw BridgeError.invalid("Run directory not found: \(runURL.path)") }
    switch command {
    case "preflight": try preflight(runURL: runURL)
    case "apply": try apply(runURL: runURL)
    case "verify": try verify(runURL: runURL)
    case "rollback-preview": try rollbackPreview(runURL: runURL)
    case "rollback": try rollback(runURL: runURL)
    default: usage()
    }
}

do {
    try main()
} catch {
    fputs("ERROR: \(error)\n", stderr)
    exit(1)
}
