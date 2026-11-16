import Charts
import Combine
import SwiftUI

/// Aba Resumo: números do período, gráfico e atalhos para Estoque, Fiado e Terminais.
struct SummaryView: View {
    @EnvironmentObject private var app: AppState
    @Environment(\.scenePhase) private var scenePhase
    @StateObject private var state = LoadState<Dashboard>()
    @State private var isVisible = false
    private let ticker = Timer.publish(every: 60, on: .main, in: .common).autoconnect()

    var body: some View {
        TenantGate(permission: nil) { context in
            LoadedScrollView(state: state, key: context, load: { try await load(context) }) { dashboard in
                DashboardContent(dashboard: dashboard)
            }
            .onReceive(ticker) { _ in
                // Atualiza sozinho só com "Hoje", app em primeiro plano e aba visível.
                guard isVisible, scenePhase == .active, context.period == .today, !state.isLoading else { return }
                Task { await state.reload(app: app) { try await load(context) } }
            }
        }
        .contextBar(showsPeriod: true)
        .navigationTitle("Resumo")
        .navigationBarTitleDisplayMode(.inline)
        .onAppear { isVisible = true }
        .onDisappear { isVisible = false }
    }

    private func load(_ context: QueryContext) async throws -> Dashboard {
        let dashboard = try await app.api.dashboard(context)
        app.updateShops(from: dashboard)
        return dashboard
    }
}

struct DashboardContent: View {
    @EnvironmentObject private var app: AppState
    let dashboard: Dashboard

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            if let sales = dashboard.sales {
                salesCards(sales)
            }
            if let payments = dashboard.payments {
                paymentsCard(payments)
            }
            if let series = dashboard.series, dashboard.sales != nil {
                SeriesChartCard(series: series, period: dashboard.period.label)
            }
            if let cash = dashboard.cash {
                linkCard(title: "Caixas", icon: "banknote", lines: [
                    Format.plural(cash.openCount, "aberto", "abertos"),
                    Format.plural(cash.shortageCount, "quebra no período", "quebras no período")
                        + (cash.shortageCount > 0 ? " (\(Format.money(cash.shortageCents)))" : ""),
                ]) { app.selectedTab = .cash }
            }
            if let stock = dashboard.stock {
                linkCard(title: "Estoque", icon: "shippingbox", lines: [
                    Format.plural(stock.zeroedCount, "produto zerado", "produtos zerados"),
                    Format.plural(stock.belowMinimumCount, "abaixo do mínimo", "abaixo do mínimo"),
                ]) { app.goToStock() }
            }
            if let credit = dashboard.credit {
                linkCard(title: "Fiado", icon: "person.2", lines: [
                    "Em aberto: " + Format.money(credit.openCents),
                    "Vencido: " + Format.money(credit.overdueCents)
                        + " (" + Format.plural(credit.overdueCustomers, "cliente", "clientes") + ")",
                ]) { app.goToMore(.credit) }
            }
            if let terminals = dashboard.terminals {
                linkCard(title: "Terminais", icon: "desktopcomputer", lines: [
                    "\(terminals.online) online de \(terminals.total)",
                    Format.plural(terminals.conflictsOpen, "conflito em aberto", "conflitos em aberto"),
                ]) { app.goToMore(.terminals) }
            }
        }
    }

    @ViewBuilder
    private func salesCards(_ sales: SalesSummary) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Faturamento · \(dashboard.period.label)").font(.subheadline).foregroundStyle(.secondary)
            Text(Format.money(sales.revenueCents))
                .font(.largeTitle.bold())
                .monospacedDigit()
                .minimumScaleFactor(0.6)
                .lineLimit(1)
            Text("Líquido de devoluções: \(Format.money(sales.netRevenueCents))")
                .font(.footnote)
                .foregroundStyle(.secondary)
        }
        .card()
        .accessibilityElement(children: .combine)

        HStack(spacing: 12) {
            metric(title: "Vendas", value: "\(sales.count)")
            metric(title: "Ticket médio", value: Format.money(sales.averageTicketCents))
        }
        metric(title: "Cancelamentos",
               value: "\(sales.cancelledCount) · \(Format.money(sales.cancelledCents))")
    }

    private func metric(title: String, value: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title).font(.subheadline).foregroundStyle(.secondary)
            Text(value).font(.title3.bold()).monospacedDigit().minimumScaleFactor(0.7).lineLimit(1)
        }
        .card()
        .accessibilityElement(children: .combine)
    }

    private func paymentsCard(_ payments: PaymentsSummary) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Recebido nas vendas").font(.headline)
            ValueRow(title: "Dinheiro", value: Format.money(payments.cashCents))
            ValueRow(title: "Pix", value: Format.money(payments.pixCents))
            ValueRow(title: "Cartão de crédito", value: Format.money(payments.cardCreditCents))
            ValueRow(title: "Cartão de débito", value: Format.money(payments.cardDebitCents))
            ValueRow(title: "Outras formas", value: Format.money(payments.otherCents))
            Divider()
            ValueRow(title: "Total", value: Format.money(payments.receivedTotalCents), emphasized: true)
            Divider()
            ValueRow(title: "Vendido no fiado", value: Format.money(payments.creditAccountCents))
            ValueRow(title: "Pago com crédito da loja", value: Format.money(payments.storeCreditCents))
            ValueRow(title: "Fiado recebido", value: Format.money(payments.creditReceivedCents))
        }
        .card()
    }

    private func linkCard(title: String, icon: String, lines: [String], action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 14) {
                Image(systemName: icon)
                    .font(.title3)
                    .foregroundStyle(PDVTheme.blue)
                    .frame(width: 32)
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 3) {
                    Text(title).font(.headline).foregroundStyle(.primary)
                    ForEach(lines, id: \.self) { line in
                        Text(line).font(.subheadline).foregroundStyle(.secondary)
                    }
                }
                Spacer()
                Image(systemName: "chevron.right").foregroundStyle(.tertiary).accessibilityHidden(true)
            }
            .card()
        }
        .buttonStyle(.plain)
        .accessibilityElement(children: .combine)
        .accessibilityHint("Abre \(title)")
    }
}

/// Gráfico de barras do faturamento por hora ou por dia.
struct SeriesChartCard: View {
    let series: Series
    let period: String

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(series.kind == "hour" ? "Faturamento por hora" : "Faturamento por dia").font(.headline)
            if series.points.allSatisfy({ $0.revenueCents == 0 }) {
                Text("Nenhuma venda concluída em \(period.lowercased()).")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            } else {
                Chart(series.points) { point in
                    BarMark(x: .value("Período", point.label),
                            y: .value("Faturamento", Double(point.revenueCents) / 100))
                        .foregroundStyle(PDVTheme.blue.gradient)
                        .accessibilityLabel(point.label)
                        .accessibilityValue("\(Format.money(point.revenueCents)), \(Format.plural(point.count, "venda", "vendas"))")
                }
                .chartXAxis {
                    AxisMarks(values: axisLabels) { _ in AxisValueLabel() }
                }
                .chartYAxis {
                    AxisMarks(position: .leading) { value in
                        AxisGridLine()
                        AxisValueLabel {
                            if let amount = value.as(Double.self) { Text(Format.money(Int(amount * 100)).replacingOccurrences(of: ",00", with: "")) }
                        }
                    }
                }
                .frame(height: 200)
            }
        }
        .card()
    }

    /// Mostra no máximo ~6 rótulos no eixo para não encavalar.
    private var axisLabels: [String] {
        let labels = series.points.map(\.label)
        guard labels.count > 7 else { return labels }
        let step = Int((Double(labels.count) / 6).rounded(.up))
        return labels.enumerated().filter { $0.offset % step == 0 }.map(\.element)
    }
}

#Preview {
    NavigationStack { SummaryView() }.environmentObject(AppState.preview())
}
