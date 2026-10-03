import Charts
import SwiftUI

struct OwnerDashboardView: View {
    @ObservedObject var session: AppSession
    @State private var showingLogout = false

    var body: some View {
        NavigationStack {
            Group {
                if let snapshot = session.snapshot {
                    dashboard(snapshot)
                } else if session.isRefreshing {
                    ProgressView("Carregando operação…")
                } else {
                    ContentUnavailableView("Sem dados", systemImage: "chart.bar.xaxis", description: Text(session.message))
                }
            }
            .background(Color(uiColor: .systemGroupedBackground))
            .navigationTitle("Monitoramento")
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(session.companyName).font(.caption.bold()).lineLimit(1)
                        Text("Visão do proprietário").font(.caption2).foregroundStyle(.secondary)
                    }
                }
                ToolbarItemGroup(placement: .topBarTrailing) {
                    Button { Task { await session.refresh() } } label: {
                        Image(systemName: session.isRefreshing ? "arrow.triangle.2.circlepath.circle" : "arrow.clockwise")
                    }
                    Button { showingLogout = true } label: { Image(systemName: "rectangle.portrait.and.arrow.right") }
                }
            }
            .refreshable { await session.refresh() }
            .confirmationDialog("Sair do monitoramento?", isPresented: $showingLogout, titleVisibility: .visible) {
                Button("Sair", role: .destructive) { Task { await session.logout() } }
                Button("Cancelar", role: .cancel) {}
            }
            .task {
                session.startMonitoring()
                await session.refresh()
            }
            .onDisappear { session.stopMonitoring() }
        }
    }

    @ViewBuilder
    private func dashboard(_ snapshot: MonitoringSnapshot) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                HStack {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Resumo de hoje").font(.title2.bold())
                        Text("Atualização automática a cada 30 segundos").font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    if let generated = snapshot.generatedAt.asDate {
                        Text(generated, style: .time).font(.caption2).foregroundStyle(.secondary)
                    }
                }
                metricGrid(snapshot)
                paymentCard(snapshot)
                terminalSalesCard(snapshot)
                terminalStatusCard(snapshot)
                if !snapshot.finance.enabled {
                    Label("O financeiro detalhado depende do recurso financeiro do plano.", systemImage: "lock.fill")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .padding(.horizontal, 4)
                }
            }
            .padding(16)
        }
    }

    private func metricGrid(_ snapshot: MonitoringSnapshot) -> some View {
        LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())], spacing: 12) {
            MetricCard(title: "Terminais online", value: "\(snapshot.terminalsOnline)/\(snapshot.terminalsTotal)", icon: "point.3.connected.trianglepath.dotted", color: .blue)
            MetricCard(title: "Faturamento hoje", value: brl(snapshot.sales.revenueCents), icon: "banknote.fill", color: .green)
            MetricCard(title: "Vendas hoje", value: "\(snapshot.sales.count)", icon: "cart.fill", color: .orange)
            MetricCard(title: "Ticket médio", value: brl(snapshot.sales.averageTicketCents), icon: "chart.line.uptrend.xyaxis", color: .purple)
        }
    }

    private func paymentCard(_ snapshot: MonitoringSnapshot) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Recebimentos por forma").font(.headline)
            HStack(spacing: 10) {
                PaymentPill(title: "Dinheiro", value: snapshot.paymentsCents.cash, icon: "banknote", color: .green)
                PaymentPill(title: "Pix", value: snapshot.paymentsCents.pix, icon: "qrcode", color: .teal)
            }
            HStack(spacing: 10) {
                PaymentPill(title: "Crédito", value: snapshot.paymentsCents.cardCredit, icon: "creditcard", color: .blue)
                PaymentPill(title: "Débito", value: snapshot.paymentsCents.cardDebit, icon: "creditcard.fill", color: .indigo)
            }
            if snapshot.finance.enabled {
                Divider()
                HStack {
                    Label("A receber", systemImage: "arrow.down.circle")
                    Spacer()
                    Text(brl(snapshot.finance.openReceivablesCents)).bold()
                }
                HStack {
                    Label("A pagar", systemImage: "arrow.up.circle")
                    Spacer()
                    Text(brl(snapshot.finance.openPayablesCents)).bold()
                }
            }
        }
        .monitorCard()
    }

    private func terminalSalesCard(_ snapshot: MonitoringSnapshot) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Vendas por terminal").font(.headline)
            if snapshot.salesByTerminal.isEmpty {
                Text("Nenhuma venda concluída hoje.").font(.subheadline).foregroundStyle(.secondary)
            } else {
                Chart(snapshot.salesByTerminal) { terminal in
                    BarMark(x: .value("Terminal", terminal.terminalName),
                            y: .value("Faturamento", Double(terminal.revenueCents) / 100))
                        .foregroundStyle(.blue.gradient)
                }
                .frame(height: 190)
                .chartYAxis { AxisMarks { value in AxisGridLine(); AxisValueLabel { if let amount = value.as(Double.self) { Text(brl(Int(amount * 100))) } } } }
                ForEach(snapshot.salesByTerminal) { terminal in
                    HStack {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(terminal.terminalName).font(.subheadline.bold())
                            Text("\(terminal.salesCount) vendas • \(terminal.shopName)").font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        Text(brl(terminal.revenueCents)).font(.subheadline.bold())
                    }
                }
            }
        }
        .monitorCard()
    }

    private func terminalStatusCard(_ snapshot: MonitoringSnapshot) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Status dos terminais").font(.headline)
            ForEach(snapshot.terminals) { terminal in
                HStack(spacing: 12) {
                    Circle().fill(terminal.isOnline ? .green : (terminal.revoked ? .red : .gray)).frame(width: 10, height: 10)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(terminal.name).font(.subheadline.bold())
                        Text("\(terminal.shopName) • \(terminalStatus(terminal))").font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    if !terminal.version.isEmpty { Text("v\(terminal.version)").font(.caption2).foregroundStyle(.secondary) }
                }
                if terminal.id != snapshot.terminals.last?.id { Divider() }
            }
        }
        .monitorCard()
    }

    private func terminalStatus(_ terminal: TerminalStatus) -> String {
        switch terminal.status { case "online": return "Online"; case "revoked": return "Revogado"; default: return "Offline" }
    }
}

private struct MetricCard: View {
    let title: String; let value: String; let icon: String; let color: Color
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Image(systemName: icon).foregroundStyle(color).font(.title3.bold())
            Text(value).font(.title3.bold()).lineLimit(1).minimumScaleFactor(0.7)
            Text(title).font(.caption).foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .monitorCard()
    }
}

private struct PaymentPill: View {
    let title: String; let value: Int; let icon: String; let color: Color
    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: icon).foregroundStyle(color)
            VStack(alignment: .leading, spacing: 2) { Text(title).font(.caption).foregroundStyle(.secondary); Text(brl(value)).font(.subheadline.bold()) }
            Spacer()
        }
        .padding(10)
        .background(Color(uiColor: .secondarySystemBackground), in: RoundedRectangle(cornerRadius: 12))
    }
}

private extension View {
    func monitorCard() -> some View {
        self.padding(16).background(.background, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
            .shadow(color: .black.opacity(0.05), radius: 10, y: 4)
    }
}

private func brl(_ cents: Int) -> String {
    let formatter = NumberFormatter()
    formatter.numberStyle = .currency
    formatter.currencyCode = "BRL"
    formatter.locale = Locale(identifier: "pt_BR")
    return formatter.string(from: NSNumber(value: Double(cents) / 100)) ?? "R$ 0,00"
}

private extension String {
    var asDate: Date? { ISO8601DateFormatter().date(from: self) }
}
