import SwiftUI
import UniformTypeIdentifiers

struct WorkoutSet: Codable, Identifiable {
    var id = UUID()
    var weightText = ""
    var repsText = ""
    var unit = "kg"
    var weightMeaning = "none"
    var completed = false

    var valid: Bool {
        guard let reps = Int(repsText), reps >= 0 else { return false }
        if weightText.trimmingCharacters(in: .whitespaces).isEmpty { return true }
        guard let weight = Double(weightText), weight.isFinite, weight >= 0 else { return false }
        return weightMeaning != "none"
    }
}

struct Exercise: Codable, Identifiable {
    var id = UUID()
    var name: String
    var category: String
    var sets: [WorkoutSet] = []
}

struct Workout: Codable, Identifiable {
    var id = UUID()
    var version = 1
    var date: String
    var exercises: [Exercise] = []
    var note = ""
    var finished = false
    var startedAt: Date? = nil
    var endedAt: Date? = nil

    var changeSignature: Data? {
        var copy = self
        copy.version = 0
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        return try? encoder.encode(copy)
    }
}

struct TrainingExport: Codable {
    var id: UUID
    var version: Int
    var date: String
    var exercises: [ExerciseExport]
    var note: String
    var startedAt: String?
    var endedAt: String?
}

struct SetExport: Codable {
    var weight: Double?
    var unit: String
    var weightMeaning: String
    var reps: Int
}

struct ExerciseExport: Codable {
    var name: String
    var category: String
    var sets: [SetExport]
}

struct Deletion: Codable, Identifiable {
    var id: UUID
    var version: Int
    var deleted = true
}

struct SavedData: Codable {
    var workouts: [Workout]
    var deletions: [Deletion]
    var acknowledged: [String: Int]?
}

struct Receipt: Decodable {
    let id: UUID
    let version: Int
    let status: String
    let message: String?
}

struct ManifestEntry: Decodable {
    let id: UUID
    let version: Int
    let deleted: Bool
}

struct ReceiptEnvelope: Decodable {
    let schema: String
    let nonce: String?
    let results: [Receipt]
    let manifest: [ManifestEntry]
    let summaryStatus: SummaryStatus?
}

struct SummaryStatus: Decodable {
    let lastGeneratedAt: String?
    let pendingMonths: Int
}

struct RecoverySet: Decodable {
    let weight: Double?
    let unit: String?
    let weightMeaning: String
    let reps: Int
}
struct RecoveryExercise: Decodable {
    let name: String
    let category: String
    let sets: [RecoverySet]
}
struct RecoveryRecord: Decodable {
    let id: UUID
    let version: Int
    let deleted: Bool?
    let date: String?
    let exercises: [RecoveryExercise]?
    let note: String?
    let startedAt: String?
    let endedAt: String?
}
struct RecoveryResponse: Decodable {
    let schema: String
    let nonce: String
    let records: [RecoveryRecord]
}

struct JSONDocument: FileDocument {
    static var readableContentTypes: [UTType] { [.json] }
    var data: Data
    init(data: Data) { self.data = data }
    init(configuration: ReadConfiguration) throws { data = configuration.file.regularFileContents ?? Data() }
    func fileWrapper(configuration: WriteConfiguration) throws -> FileWrapper { FileWrapper(regularFileWithContents: data) }
}

@MainActor final class WorkoutStore: ObservableObject {
    @Published var workouts: [Workout] = []
    @Published var deletions: [Deletion] = []
    @Published var acknowledged: [String: Int] = [:]
    @Published var error: String?
    @Published var storageAvailable = true
    private var loadFailed = false
    private let url: URL = {
        let test = ProcessInfo.processInfo.arguments.contains("--ui-testing")
        return FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0].appendingPathComponent(test ? "workouts-ui-test.json" : "workouts.json")
    }()

    init() {
        if ProcessInfo.processInfo.arguments.contains("--ui-testing") && ProcessInfo.processInfo.arguments.contains("--ui-reset") {
            try? FileManager.default.removeItem(at: url)
            PairingStorage.resetTestIdentity()
        }
        guard FileManager.default.fileExists(atPath: url.path) else { return }
        do {
            let data = try Data(contentsOf: url)
            if let saved = try? JSONDecoder().decode(SavedData.self, from: data) {
                workouts = saved.workouts
                deletions = saved.deletions
                acknowledged = saved.acknowledged ?? [:]
            } else {
                workouts = try JSONDecoder().decode([Workout].self, from: data)
            }
        }
        catch { loadFailed = true; storageAvailable = false; self.error = "无法读取训练记录，已停止写入以保护原文件：\(error.localizedDescription)" }
    }

    @discardableResult func save() -> Bool {
        guard !loadFailed else { error = "训练记录读取失败，已停止写入以保护原文件。"; return false }
        do {
            let data = try JSONEncoder().encode(SavedData(workouts: workouts, deletions: deletions, acknowledged: acknowledged))
            try data.write(to: url, options: [.atomic, .completeFileProtectionUnlessOpen])
            storageAvailable = true
            return true
        } catch { storageAvailable = false; self.error = "保存失败：\(error.localizedDescription)"; return false }
    }

    func installRecovery(workouts newWorkouts: [Workout], deletions newDeletions: [Deletion]) throws {
        guard !loadFailed, workouts.isEmpty, deletions.isEmpty else { throw RecoveryError.localData }
        let acknowledgements = Dictionary(uniqueKeysWithValues: newWorkouts.map { ($0.id.uuidString, $0.version) } + newDeletions.map { ($0.id.uuidString, $0.version) })
        let data = try JSONEncoder().encode(SavedData(workouts: newWorkouts, deletions: newDeletions, acknowledged: acknowledgements))
        try data.write(to: url, options: [.atomic, .completeFileProtectionUnlessOpen])
        workouts = newWorkouts
        deletions = newDeletions
        acknowledged = acknowledgements
    }

    func delete(at offsets: IndexSet) {
        for index in offsets { deletions.append(Deletion(id: workouts[index].id, version: workouts[index].version + 1)) }
        workouts.remove(atOffsets: offsets)
        save()
    }

    func today() -> UUID {
        let day = Self.dateString(Date())
        if let existing = workouts.first(where: { $0.date == day && !$0.finished }) { return existing.id }
        let workout = Workout(date: day, startedAt: Date())
        workouts.insert(workout, at: 0)
        save()
        return workout.id
    }

    static func dateString(_ date: Date) -> String {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd"
        return f.string(from: date)
    }
}

struct Preset: Identifiable {
    let id: String
    let symbol: String
    let meaning: String
}

let presets: [String: [Preset]] = [
    "chest": [.init(id: "杠铃卧推", symbol: "figure.strengthtraining.traditional", meaning: "total"), .init(id: "哑铃卧推", symbol: "dumbbell", meaning: "per_side"), .init(id: "器械推胸", symbol: "figure.strengthtraining.traditional", meaning: "total"), .init(id: "绳索夹胸", symbol: "figure.strengthtraining.functional", meaning: "total")],
    "back": [.init(id: "高位下拉", symbol: "figure.strengthtraining.traditional", meaning: "total"), .init(id: "坐姿划船", symbol: "figure.rower", meaning: "total"), .init(id: "引体向上", symbol: "figure.climbing", meaning: "none")],
    "legs": [.init(id: "杠铃深蹲", symbol: "figure.strengthtraining.traditional", meaning: "total"), .init(id: "腿举", symbol: "figure.strengthtraining.functional", meaning: "total"), .init(id: "徒手深蹲", symbol: "figure.strengthtraining.functional", meaning: "none")]
]

let exerciseArt: [String: String] = [
    "杠铃卧推": "bench-press", "哑铃卧推": "dumbbell-bench-press", "器械推胸": "machine-chest-press", "绳索夹胸": "cable-fly",
    "高位下拉": "lat-pulldown", "坐姿划船": "seated-row", "引体向上": "pull-up",
    "杠铃深蹲": "squat", "腿举": "leg-press", "徒手深蹲": "bodyweight-squat"
]

@main struct FitnessApp: App {
    @StateObject private var store = WorkoutStore()
    var body: some Scene {
        WindowGroup {
            ContentView().environmentObject(store)
                .alert("记录问题", isPresented: Binding(get: { store.error != nil }, set: { if !$0 { store.error = nil } })) { Button("知道了") { store.error = nil } } message: { Text(store.error ?? "") }
        }
    }
}

struct ContentView: View {
    @EnvironmentObject var store: WorkoutStore
    var body: some View {
        TabView {
            NavigationStack { TodayView() }.tabItem { Label("今天", systemImage: "square.grid.2x2") }
            NavigationStack { HistoryView() }.tabItem { Label("历史", systemImage: "clock") }
            NavigationStack { SyncView() }.tabItem { Label("同步", systemImage: "arrow.triangle.2.circlepath") }
        }.tint(Color(red: 0.08, green: 0.36, blue: 0.75))
    }
}

struct TodayView: View {
    @EnvironmentObject var store: WorkoutStore
    let categories: [(String, String, String)] = [("chest", "练胸", "figure.strengthtraining.traditional"), ("back", "练背", "figure.rower"), ("legs", "练腿", "figure.strengthtraining.functional")]
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                Text(Date.now, format: .dateTime.month().day().weekday()).font(.subheadline).foregroundStyle(.secondary)
                Text("今天练什么？").font(.largeTitle.bold())
                ForEach(categories, id: \.0) { item in
                    NavigationLink { ExercisePicker(category: item.0, title: item.1) } label: {
                        HStack(spacing: 18) {
                            Image(systemName: item.2).font(.system(size: 30)).frame(width: 55, height: 55).foregroundStyle(.blue)
                            Text(item.1).font(.title3.bold())
                            Spacer()
                            Image(systemName: "chevron.right").foregroundStyle(.secondary)
                        }.padding(18).background(.background, in: RoundedRectangle(cornerRadius: 16))
                    }.buttonStyle(.plain)
                }
                Text("今日训练").font(.title2.bold())
                let today = WorkoutStore.dateString(Date())
                let entries = store.workouts.filter { $0.date == today }
                if entries.isEmpty { Text("还没有记录。选择一个部位开始。 ").foregroundStyle(.secondary) }
                ForEach(entries) { workout in
                    NavigationLink { WorkoutDetail(id: workout.id) } label: {
                        HStack { VStack(alignment: .leading, spacing: 6) {
                            Text(workout.finished ? "已完成" : "正在记录").font(.headline)
                            Text(workout.exercises.map(\.name).joined(separator: "、")).font(.subheadline).lineLimit(2)
                            Text("\(workout.exercises.count) 个动作 · \(workout.exercises.reduce(0) { $0 + $1.sets.filter(\.completed).count }) 组")
                                .font(.footnote).foregroundStyle(.secondary)
                        }; Spacer(); Image(systemName: "chevron.right") }.padding().background(.background, in: RoundedRectangle(cornerRadius: 14))
                    }.buttonStyle(.plain)
                }
            }.padding(20)
        }.background(Color(uiColor: .systemGroupedBackground)).navigationTitle("训练").navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .topBarTrailing) { NavigationLink("同步") { SyncView() } } }
    }
}

struct ExercisePicker: View {
    @EnvironmentObject var store: WorkoutStore
    let category: String
    let title: String
    @State private var customName = ""
    @State private var selected: UUID?
    var body: some View {
        ScrollView {
            LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())], spacing: 12) {
                ForEach(presets[category] ?? []) { preset in
                    Button { add(preset) } label: {
                        VStack(alignment: .leading, spacing: 14) {
                            if let art = exerciseArt[preset.id],
                               let path = Bundle.main.path(forResource: art, ofType: "png", inDirectory: "ExerciseArt"),
                               let illustration = UIImage(contentsOfFile: path) {
                                Image(uiImage: illustration).renderingMode(.template).resizable().scaledToFit().foregroundStyle(Color(red: 0.10, green: 0.31, blue: 0.55))
                                    .frame(maxWidth: .infinity).frame(height: 105).accessibilityHidden(true)
                            } else {
                                Image(systemName: preset.symbol).font(.system(size: 40)).frame(maxWidth: .infinity, minHeight: 105).foregroundStyle(.blue).accessibilityHidden(true)
                            }
                            Text(preset.id).font(.headline).foregroundStyle(.primary)
                            Text("点击记录").font(.caption).foregroundStyle(.secondary)
                        }.padding().frame(maxWidth: .infinity, alignment: .leading).background(.background, in: RoundedRectangle(cornerRadius: 14))
                    }.buttonStyle(.plain)
                }
            }
            HStack { TextField("自定义动作名称", text: $customName).textFieldStyle(.roundedBorder); Button("添加") { add(.init(id: customName.trimmingCharacters(in: .whitespacesAndNewlines), symbol: "dumbbell", meaning: "none")) }.disabled(customName.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty) }.padding(.top, 20)
            NavigationLink("图示来源与许可") { ExerciseCreditsView() }.font(.footnote).padding(.top, 12)
        }.padding().background(Color(uiColor: .systemGroupedBackground)).navigationTitle(title)
            .navigationDestination(item: $selected) { id in WorkoutDetail(id: id) }
    }
    func add(_ preset: Preset) {
        let id = store.today()
        guard let i = store.workouts.firstIndex(where: { $0.id == id }) else { return }
        store.workouts[i].exercises.append(Exercise(name: preset.id, category: category, sets: [WorkoutSet(weightMeaning: preset.meaning)]))
        store.workouts[i].version += 1
        store.save()
        selected = id
    }
}

struct ArtCredits: Decodable {
    struct Credit: Decodable, Identifiable {
        struct Attribution: Decodable {
            struct Source: Decodable { let name: String; let url: String; let changes: String? }
            let creator: String
            let creatorUrl: String
            let source: Source?
        }
        let slug: String
        let upstream_png: String
        let attribution: Attribution
        var id: String { slug }
    }
    let artworks: [Credit]
}

struct ExerciseCreditsView: View {
    private var credits: [ArtCredits.Credit] {
        guard let url = Bundle.main.url(forResource: "EXERCISE_ART_ATTRIBUTION", withExtension: "json"),
              let data = try? Data(contentsOf: url), let value = try? JSONDecoder().decode(ArtCredits.self, from: data) else { return [] }
        return value.artworks
    }
    var body: some View {
        List {
            Section {
                Text("动作图示来自 Workout Guide，作者 Bryl Lim；部分图示改编自 Everkinetic。许可：CC BY-SA 4.0。本应用仅在显示时改变线条颜色，源 PNG 未修改。图示用于辨认动作，不是动作指导。")
                Link("查看 CC BY-SA 4.0 许可", destination: URL(string: "https://creativecommons.org/licenses/by-sa/4.0/")!)
                Link("Workout Guide 原始素材", destination: URL(string: "https://github.com/bryllim/workout-guide")!)
            }
            ForEach(credits) { credit in
                Section(credit.slug) {
                    if let url = URL(string: credit.upstream_png) { Link("原图与作者 \(credit.attribution.creator)", destination: url) }
                    if let source = credit.attribution.source, let url = URL(string: source.url) {
                        Link("上游来源：\(source.name)", destination: url)
                        if let changes = source.changes { Text(changes).font(.footnote).foregroundStyle(.secondary) }
                    }
                }
            }
        }.navigationTitle("图示来源与许可")
    }
}

struct WorkoutDetail: View {
    @EnvironmentObject var store: WorkoutStore
    let id: UUID
    @State private var removingExercise: UUID?
    @State private var showingRemoveConfirmation = false
    var body: some View {
        if let index = store.workouts.firstIndex(where: { $0.id == id }) {
            Form {
                ForEach($store.workouts[index].exercises) { $exercise in
                    Section(exercise.name) {
                        ForEach($exercise.sets) { $set in
                            VStack(alignment: .leading) {
                                Text("第 \((exercise.sets.firstIndex(where: { $0.id == set.id }) ?? 0) + 1) 组")
                                    .font(.subheadline.weight(.semibold)).foregroundStyle(.secondary)
                                HStack {
                                    TextField("重量 kg（可空）", text: $set.weightText).keyboardType(.decimalPad)
                                    TextField("次数", text: $set.repsText).keyboardType(.numberPad)
                                    Toggle("完成", isOn: $set.completed).labelsHidden().disabled(!set.valid && !set.completed)
                                }
                                Text(set.completed ? "已完成此组" : "填写后打开开关，标记此组完成")
                                    .font(.caption).foregroundStyle(.secondary)
                                if !set.weightText.isEmpty {
                                    Picker("重量含义", selection: $set.weightMeaning) { Text("请选择").tag("none"); Text("总重").tag("total"); Text("单只").tag("per_side"); Text("额外负重").tag("additional") }
                                }
                                if !set.valid { Text("填写次数；如填写重量，请同时选择重量含义。").font(.caption).foregroundStyle(.secondary) }
                            }
                        }.onDelete { exercise.sets.remove(atOffsets: $0) }
                        HStack {
                            Button("添加一组") { exercise.sets.append(WorkoutSet()) }
                            Spacer()
                            Button("复制上一组") {
                                guard let previous = exercise.sets.last else { return }
                                var copy = previous
                                copy.id = UUID()
                                copy.completed = false
                                exercise.sets.append(copy)
                            }.disabled(exercise.sets.isEmpty)
                        }
                        Button("移除这个动作", role: .destructive) {
                            removingExercise = exercise.id
                            showingRemoveConfirmation = true
                        }
                    }
                }
                Section("备注") { TextField("可选", text: $store.workouts[index].note, axis: .vertical) }
                Section {
                    if store.workouts[index].exercises.contains(where: { $0.sets.contains(where: { !$0.completed }) }) {
                        Text("未标记完成的组不会同步到 Mac。") .font(.footnote).foregroundStyle(.secondary)
                    }
                    Button(store.workouts[index].finished ? "继续编辑" : "完成训练") {
                    if !store.workouts[index].finished && store.workouts[index].endedAt == nil { store.workouts[index].endedAt = Date() }
                    store.workouts[index].finished.toggle()
                } }
            }.navigationTitle(store.workouts[index].date)
                .confirmationDialog("移除这个动作及其所有组？", isPresented: $showingRemoveConfirmation, titleVisibility: .visible) {
                    Button("移除动作", role: .destructive) {
                        guard let exerciseID = removingExercise,
                              let currentIndex = store.workouts.firstIndex(where: { $0.id == id }) else { return }
                        store.workouts[currentIndex].exercises.removeAll { $0.id == exerciseID }
                        removingExercise = nil
                    }
                    Button("取消", role: .cancel) { removingExercise = nil }
                }
                .onChange(of: store.workouts[index].changeSignature) { _, _ in
                    store.workouts[index].version += 1
                    store.save()
                }
        } else { ContentUnavailableView("找不到训练", systemImage: "exclamationmark.triangle") }
    }
}

struct HistoryView: View {
    @EnvironmentObject var store: WorkoutStore
    @State private var pendingDeletion: [UUID] = []
    @State private var showingDeleteConfirmation = false
    var body: some View {
        List {
            ForEach(store.workouts) { workout in
                NavigationLink { WorkoutDetail(id: workout.id) } label: {
                    VStack(alignment: .leading) {
                        Text(workout.date).font(.headline)
                        Text("\(workout.exercises.count) 个动作 · \(workout.finished ? "已完成" : "进行中")").foregroundStyle(.secondary)
                    }
                }
            }.onDelete { offsets in
                pendingDeletion = offsets.map { store.workouts[$0].id }
                showingDeleteConfirmation = true
            }
        }.navigationTitle("历史")
            .confirmationDialog("删除训练及其所有组？下次同步会将删除同步到 Mac。", isPresented: $showingDeleteConfirmation, titleVisibility: .visible) {
                Button("删除训练", role: .destructive) {
                    let ids = Set(pendingDeletion)
                    let offsets = IndexSet(store.workouts.indices.filter { ids.contains(store.workouts[$0].id) })
                    store.delete(at: offsets)
                    pendingDeletion = []
                }
                Button("取消", role: .cancel) { pendingDeletion = [] }
            }
    }
}

struct SyncView: View {
    @EnvironmentObject var store: WorkoutStore
    @State private var document = JSONDocument(data: Data())
    @State private var exporting = false
    @State private var message: String?
    @State private var failures: [String] = []
    @State private var summaryStatus: SummaryStatus?
    @State private var host = ""
    @State private var port = ""
    @State private var fingerprint = ""
    @State private var secret = ""
    @State private var paired = PairingStorage.read() != nil
    @State private var busy = false
    @State private var showingScanner = false
    var body: some View {
        Form {
            if let message { Section { Text(message).foregroundStyle(.secondary) } }
            if !failures.isEmpty {
                Section("需要检查的记录") {
                    ForEach(failures, id: \.self) { failure in Text(failure).font(.footnote) }
                }
            }
            Section("Mac 配对") {
                if paired { Text("已保存配对信息").foregroundStyle(.secondary) }
                Button("扫描 Mac 配对码") { showingScanner = true }
                DisclosureGroup("手动输入配对信息") {
                    TextField("Mac 本地地址", text: $host).textInputAutocapitalization(.never).autocorrectionDisabled()
                    TextField("端口", text: $port).keyboardType(.numberPad)
                    TextField("证书 SHA-256 指纹", text: $fingerprint).textInputAutocapitalization(.never).autocorrectionDisabled()
                    SecureField("配对密钥", text: $secret).textInputAutocapitalization(.never).autocorrectionDisabled().onSubmit { savePairing() }
                    Button("保存手动配对信息") { savePairing() }
                }
                Button(busy ? "正在同步…" : "与 Mac 同步") { Task { await syncNow() } }.disabled(!paired || busy)
            }
            Section("训练数据") {
                Text("导出已完成的训练，再在 Mac 上导入。文件包含私人训练数据。")
                Button("导出训练文件") { export() }
                let completed = store.workouts.filter(\.finished)
                let accepted = completed.filter { store.acknowledged[$0.id.uuidString] == $0.version }.count
                Text("已收录 \(accepted) / \(completed.count) 次已完成训练。导出本身不代表已收录。") .font(.footnote).foregroundStyle(.secondary)
            }
            Section("本地月度摘要") {
                if let status = summaryStatus {
                    if let generated = status.lastGeneratedAt {
                        Text("上次生成（UTC）：\(generated)")
                    } else {
                        Text("尚未生成摘要")
                    }
                    Text("待生成或更新 \(status.pendingMonths) 个月")
                    Text("这是最近一次同步时的生成状态；知识库索引在 Mac 后台更新。")
                        .font(.footnote).foregroundStyle(.secondary)
                } else {
                    Text("同步后显示摘要状态。") .foregroundStyle(.secondary)
                }
            }
            Section("恢复") {
                Text("换手机时请先在 Mac 选择“新设备配对”，这会撤销旧手机访问。仅可在本机没有训练记录时恢复；未同步到 Mac 的记录无法恢复。")
                Button(busy ? "正在处理…" : "从 Mac 恢复") { Task { await recoverNow() } }
                    .disabled(!paired || busy || !store.workouts.isEmpty || !store.deletions.isEmpty)
            }
        }.navigationTitle("同步")
            .sheet(isPresented: $showingScanner) {
                NavigationStack {
                    PairScanner { code in
                        showingScanner = false
                        do {
                            let encoded = String(code.dropFirst("fitness-pair-v1:".count))
                            guard encoded.count <= 4096, let data = Data(base64Encoded: encoded) else { throw SyncError.badPairing }
                            let pairing = try JSONDecoder().decode(Pairing.self, from: data)
                            try PairingStorage.save(pairing)
                            paired = true
                            message = "Mac 配对信息已保存。"
                        } catch { message = "配对码无效：\(error.localizedDescription)" }
                    }.ignoresSafeArea().navigationTitle("扫描 Mac 配对码").navigationBarTitleDisplayMode(.inline)
                        .toolbar { ToolbarItem(placement: .cancellationAction) { Button("取消") { showingScanner = false } } }
                }
            }
            .fileExporter(isPresented: $exporting, document: document, contentType: .json, defaultFilename: "fitness-export") { result in
                if case .failure(let error) = result { message = "导出失败：\(error.localizedDescription)" }
                else { message = "文件已导出；尚未确认 Mac 收录。" }
            }
    }

    func export() {
        do {
            let records = try serializedRecords()
            document = JSONDocument(data: try JSONSerialization.data(withJSONObject: records))
            exporting = true
        } catch { message = error.localizedDescription }
    }

    func savePairing() {
        do {
            let pairing = Pairing(address: host, port: Int(port) ?? 0, fingerprint: fingerprint.lowercased(), secret: secret)
            try PairingStorage.save(pairing)
            secret = ""
            paired = true
            message = "配对已保存在本机钥匙串；请启动 Mac 接收端后同步。"
        } catch { message = error.localizedDescription }
    }

    func serializedRecords() throws -> [Any] {
        guard store.storageAvailable else { throw RecordInputError.storage }
        let completedWorkouts = store.workouts.filter(\.finished)
        guard completedWorkouts.allSatisfy({ $0.exercises.allSatisfy { $0.sets.allSatisfy { !$0.completed || $0.valid } } }) else {
            throw RecordInputError.invalid
        }
        let workouts = completedWorkouts.map { workout in
            TrainingExport(id: workout.id, version: workout.version, date: workout.date,
                           exercises: workout.exercises.map { exercise in
                ExerciseExport(name: exercise.name, category: exercise.category,
                    sets: exercise.sets.filter(\.completed).map { set in
                        let weight = Double(set.weightText)
                        return SetExport(weight: weight, unit: set.unit, weightMeaning: weight == nil ? "none" : set.weightMeaning, reps: Int(set.repsText) ?? 0)
                    })
            }, note: workout.note,
               startedAt: workout.startedAt.map { ISO8601DateFormatter().string(from: $0) },
               endedAt: workout.endedAt.map { ISO8601DateFormatter().string(from: $0) })
        }
        let encoder = JSONEncoder()
        let workoutObjects = try JSONSerialization.jsonObject(with: encoder.encode(workouts)) as? [Any] ?? []
        let deletionObjects = try JSONSerialization.jsonObject(with: encoder.encode(store.deletions)) as? [Any] ?? []
        return workoutObjects + deletionObjects
    }

    @MainActor func syncNow() async {
        guard let pairing = PairingStorage.read() else { message = "请先配对 Mac。"; return }
        failures = []
        summaryStatus = nil
        busy = true
        defer { busy = false }
        do {
            let nonce = UUID().uuidString + UUID().uuidString
            let outgoing = try serializedRecords()
            let expected = try outgoing.map { item -> String in
                guard let object = item as? [String: Any], let id = object["id"] as? String,
                      let uuid = UUID(uuidString: id), let version = object["version"] as? Int else { throw SyncError.response }
                return "\(uuid.uuidString):\(version)"
            }
            guard Set(expected).count == expected.count else { throw SyncError.response }
            let data = try await SecureSync.post(pairing: pairing, endpoint: "sync", body: ["nonce": nonce, "records": outgoing])
            let receipt = try JSONDecoder().decode(ReceiptEnvelope.self, from: data)
            guard receipt.schema == "fitness-receipt-v2", receipt.nonce == nonce else { throw SyncError.nonce }
            let returned = receipt.results.map { "\($0.id.uuidString):\($0.version)" }
            guard returned.count == expected.count, Set(returned).count == returned.count,
                  Set(returned) == Set(expected) else { throw SyncError.response }
            guard Set(receipt.manifest.map(\.id)).count == receipt.manifest.count else { throw SyncError.response }
            let manifestByID = Dictionary(uniqueKeysWithValues: receipt.manifest.map { ($0.id, $0) })
            summaryStatus = receipt.summaryStatus
            let previousAcknowledgements = store.acknowledged
            var accepted = 0
            var failed = 0
            for row in receipt.results {
                guard row.status == "accepted" || row.status == "duplicate" else {
                    failed += 1
                    if store.acknowledged[row.id.uuidString] == row.version {
                        store.acknowledged.removeValue(forKey: row.id.uuidString)
                    }
                    let reason = row.message ?? (row.status == "stale" ? "Mac 已有更新版本；请先检查对账差异。" : "接收端未确认，请检查数据。")
                    failures.append("记录 \(row.id.uuidString.prefix(8))，版本 \(row.version)：\(reason)")
                    continue
                }
                let workout = store.workouts.first { $0.id == row.id && $0.version == row.version }
                let deletion = store.deletions.first { $0.id == row.id && $0.version == row.version }
                guard workout != nil || deletion != nil else {
                    failed += 1
                    failures.append("记录 \(row.id.uuidString.prefix(8)) 在同步期间已变化，请再次同步。")
                    continue
                }
                guard let remoteRecord = manifestByID[row.id], remoteRecord.version == row.version,
                      remoteRecord.deleted == (deletion != nil) else {
                    failed += 1
                    if store.acknowledged[row.id.uuidString] == row.version {
                        store.acknowledged.removeValue(forKey: row.id.uuidString)
                    }
                    failures.append("记录 \(row.id.uuidString.prefix(8)) 的回执与 Mac 清单不一致，请再次同步。")
                    continue
                }
                store.acknowledged[row.id.uuidString] = row.version
                accepted += 1
            }
            guard store.save() else { store.acknowledged = previousAcknowledgements; throw RecordInputError.storage }
            let local = store.workouts.map { "\($0.id.uuidString):\($0.version):false" } + store.deletions.map { "\($0.id.uuidString):\($0.version):true" }
            let remote = receipt.manifest.map { "\($0.id.uuidString):\($0.version):\($0.deleted)" }
            let differences = Set(local).symmetricDifference(Set(remote)).count
            message = "已确认 \(accepted) 条，\(failed) 条需检查。ID 与版本对账：\(differences == 0 ? "一致" : "有差异")。"
        } catch { message = "同步失败：\(error.localizedDescription)" }
    }

    @MainActor func recoverNow() async {
        guard let pairing = PairingStorage.read() else { message = "请先配对 Mac。"; return }
        guard store.workouts.isEmpty && store.deletions.isEmpty else { message = "手机已有记录，不能覆盖恢复。"; return }
        busy = true
        defer { busy = false }
        do {
            let nonce = UUID().uuidString + UUID().uuidString
            let data = try await SecureSync.post(pairing: pairing, endpoint: "recovery", body: ["nonce": nonce])
            let response = try JSONDecoder().decode(RecoveryResponse.self, from: data)
            guard response.schema == "fitness-recovery-v2", response.nonce == nonce else { throw SyncError.nonce }
            guard Set(response.records.map(\.id)).count == response.records.count else { throw RecoveryError.invalid }
            var workouts: [Workout] = []
            var deletions: [Deletion] = []
            for record in response.records {
                guard record.version > 0 else { throw RecoveryError.invalid }
                if record.deleted == true { deletions.append(Deletion(id: record.id, version: record.version)); continue }
                guard let date = record.date, validDate(date), let exercises = record.exercises else { throw RecoveryError.invalid }
                var values: [Exercise] = []
                for exercise in exercises {
                    guard !exercise.name.trimmingCharacters(in: .whitespaces).isEmpty,
                          ["chest", "back", "legs", "other"].contains(exercise.category) else { throw RecoveryError.invalid }
                    let sets = try exercise.sets.map { item -> WorkoutSet in
                        guard item.reps >= 0 else { throw RecoveryError.invalid }
                        if let weight = item.weight {
                            guard weight.isFinite, weight >= 0, item.unit == "kg", ["total", "per_side", "additional"].contains(item.weightMeaning) else { throw RecoveryError.invalid }
                        } else if item.weightMeaning != "none" { throw RecoveryError.invalid }
                        return WorkoutSet(weightText: item.weight.map { String($0) } ?? "", repsText: String(item.reps), unit: "kg", weightMeaning: item.weightMeaning, completed: true)
                    }
                    values.append(Exercise(name: exercise.name, category: exercise.category, sets: sets))
                }
                let formatter = ISO8601DateFormatter()
                let start = record.startedAt.flatMap { formatter.date(from: $0) }
                let end = record.endedAt.flatMap { formatter.date(from: $0) }
                if (record.startedAt != nil && start == nil) || (record.endedAt != nil && end == nil) || (start != nil && end != nil && end! < start!) { throw RecoveryError.invalid }
                workouts.append(Workout(id: record.id, version: record.version, date: date, exercises: values, note: record.note ?? "", finished: true, startedAt: start, endedAt: end))
            }
            try store.installRecovery(workouts: workouts, deletions: deletions)
            message = "已恢复 \(workouts.count) 次训练；删除状态也已恢复。请核对记录。"
        } catch { message = "恢复失败，未覆盖手机记录：\(error.localizedDescription)" }
    }

    func validDate(_ string: String) -> Bool {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.calendar = Calendar(identifier: .gregorian)
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        formatter.dateFormat = "yyyy-MM-dd"
        formatter.isLenient = false
        guard let date = formatter.date(from: string) else { return false }
        return formatter.string(from: date) == string
    }
}

enum RecordInputError: LocalizedError {
    case invalid, storage
    var errorDescription: String? {
        switch self {
        case .invalid: return "已完成的组有无效输入，请返回训练记录修正。"
        case .storage: return "本地保存不可用，不能同步；请先解决存储问题。"
        }
    }
}

enum RecoveryError: LocalizedError {
    case localData, invalid
    var errorDescription: String? {
        switch self {
        case .localData: return "手机已有记录或本地存储不可用，未覆盖。"
        case .invalid: return "Mac 返回的恢复记录格式无效。"
        }
    }
}
