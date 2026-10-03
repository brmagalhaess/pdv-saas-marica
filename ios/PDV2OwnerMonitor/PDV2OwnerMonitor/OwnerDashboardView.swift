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
                    ProgressView("Carregando operação…").font(.subheadline)
                } else {
                    ContentUnavailableView("Não foi possível carregar", systemImage: "wifi.exclamationmark",
                                           description: Text(session.message.isEmpty ? "Puxe para tentar novamente." : session.message))
                }
            }
            .background(Color(uiColor: .systemGroupedBackground))
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    HStack(spacing: 9) {
                        BrandMark(size: 34)
                        VStack(alignment: .leading, spacing: 1) {
                            Text("PDV2 MONITOR").font(.caption2.weight(.bold)).tracking(1.2)
                            Text(session.companyName).font(.caption.weight(.semibold)).lineLimit(1)
                        }
                    }
                }
                ToolbarItemGroup(placement: .topBarTrailing) {
                    Button { Task { await session.refresh() } } label: {
                        Image(systemName: session.isRefreshing ? "arrow.triangle.2.circlepath" : "arrow.clockwise")
                    }
                    .accessibilityLabel("Atualizar indicadores")
                    Button { showingLogout = true } label: { Image(systemName: "person.crop.circle.badge.xmark") }
                        .accessibilityLabel("Sair")
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
        ScrollView(showsIndicators: false) {
            VStack(alignment: .leading, spacing: 18) {
                dashboardHeading(snapshot)
                heroCard(snapshot)
                metricGrid(snapshot)
                paymentCard(snapshot)
                terminalSalesCard(snapshot)
                terminalStatusCard(snapshot)
                Text("Atualização automática a cada 30 segundos • Dados somente leitura")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 4)
            }
            .padding(.horizontal, 16)
            .padding(.top, 18)
            .padding(.bottom, 28)
        }
    }

    private func dashboardHeading(_ snapshot: MonitoringSnapshot) -> some View {
        HStack(alignment: .bottom) {
            VStack(alignment: .leading, spacing: 4) {
                Text("Olá, proprietário").font(.subheadline.weight(.medium)).foregroundStyle(PDVTheme.muted)
                Text("Visão geral").font(.system(size: 30, weight: .bold, design: .rounded)).foregroundStyle(PDVTheme.ink)
            }
            Spacer()
            VStack(alignment: .trailing, spacing: 3) {
                Text(snapshot.period.date.asBrazilDate).font(.caption.weight(.semibold))
                Text("atualizado \(shortDate(snapshot.generatedAt))").font(.caption2).foregroundStyle(.secondary)
            }
        }
    }

    private func heroCard(_ snapshot: MonitoringSnapshot) -> some View {
        VStack(alignment: .leading, spacing: 20) {
            HStack {
                Label("Faturamento de hoje", systemImage: "waveform.path.ecg")
                    .font(.subheadline.weight(.semibold)).foregroundStyle(.white.opacity(0.82))
                Spacer()
                Text("AO VIVO")
                    .font(.system(size: 10, weight: .bold, design: .rounded)).tracking(1.1)
                    .padding(.horizontal, 9).padding(.vertical, 6)
                    .background(.white.opacity(0.14), in: Capsule())
            }
            Text(brl(snapshot.sales.revenueCents))
                .font(.system(size: 38, weight: .bold, design: .rounded)).foregroundStyle(.white)
                .minimumScaleFactor(0.7)
            HStack(spacing: 0) {
                HeroStat(title: "Vendas", value: "\(snapshot.sales.count)")
                Divider().overlay(.white.opacity(0.28)).frame(height: 32)
                HeroStat(title: "Ticket médio", value: brl(snapshot.sales.averageTicketCents))
                Spacer()
                Image(systemName: "chart.line.uptrend.xyaxis")
                    .font(.system(size: 42, weight: .semibold)).foregroundStyle(PDVTheme.mint.opacity(0.9))
            }
        }
        .padding(22)
        .background(LinearGradient(colors: [PDVTheme.navy, PDVTheme.blueDeep], startPoint: .topLeading, endPoint: .bottomTrailing),
                    in: RoundedRectangle(cornerRadius: 24, style: .continuous))
        .overlay(alignment: .bottomTrailing) {
            Circle().fill(.white.opacity(0.06)).frame(width: 150, height: 150).offset(x: 45, y: 65)
        }
        .clipped()
        .shadow(color: PDVTheme.navy.opacity(0.22), radius: 18, y: 9)
    }

    private func metricGrid(_ snapshot: MonitoringSnapshot) -> some View {
        let received = snapshot.paymentsCents.cash + snapshot.paymentsCents.pix + snapshot.paymentsCents.cardCredit + snapshot.paymentsCents.cardDebit + snapshot.paymentsCents.other
        return LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())], spacing: 12) {
            MetricCard(title: "Terminais online", value: "\(snapshot.terminalsOnline)/\(snapshot.terminalsTotal)", icon: "point.3.connected.trianglepath.dotted", color: PDVTheme.blue)
            MetricCard(title: "Vendas concluídas", value: "\(snapshot.sales.count)", icon: "checkmark.seal.fill", color: .orange)
            MetricCard(title: "Recebimentos", value: brl(received), icon: "arrow.down.circle.fill", color: .green)
            MetricCard(title: "Status da rede", value: snapshot.terminalsOnline == snapshot.terminalsTotal ? "Estável" : "Atenção", icon: "antenna.radiowaves.left.and.right", color: snapshot.terminalsOnline == snapshot.terminalsTotal ? .green : .orange)
        }
    }

    private func paymentCard(_ snapshot: MonitoringSnapshot) -> some View {
        let payments: [(String, String, Int, Color)] = [
            ("Dinheiro", "banknote.fill", snapshot.paymentsCents.cash, .green),
            ("Pix", "qrcode", snapshot.paymentsCents.pix, .teal),
            ("Crédito", "creditcard.fill", snapshot.paymentsCents.cardCredit, .blue),
            ("Débito", "creditcard", snapshot.paymentsCents.cardDebit, .indigo)
        ]
        let maximum = max(1, payments.map(\.2).max() ?? 1)
        return VStack(alignment: .leading, spacing: 15) {
            sectionTitle("Recebimentos", subtitle: "Total por forma de pagamento", icon: "rectangle.and.hand.point.up.left.fill")
            ForEach(payments, id: \.0) { payment in
                HStack(spacing: 12) {
                    Image(systemName: payment.1).font(.subheadline.bold()).foregroundStyle(payment.3).frame(width: 20)
                    VStack(alignment: .leading, spacing: 7) {
                        HStack { Text(payment.0).font(.subheadline.weight(.medium)); Spacer(); Text(brl(payment.2)).font(.subheadline.bold()) }
                        ProgressView(value: Double(payment.2), total: Double(maximum)).tint(payment.3)
                    }
                }
            }
            if snapshot.finance.enabled {
                Divider().padding(.vertical, 2)
                HStack { Label("A receber", systemImage: "arrow.down.circle"); Spacer(); Text(brl(snapshot.finance.openReceivablesCents)).bold() }
                HStack { Label("A pagar", systemImage: "arrow.up.circle"); Spacer(); Text(brl(snapshot.finance.openPayablesCents)).bold() }
            }
        }
        .monitorCard()
    }

    private func terminalSalesCard(_ snapshot: MonitoringSnapshot) -> some View {
        VStack(alignment: .leading, spacing: 15) {
            sectionTitle("Desempenho dos caixas", subtitle: "Faturamento por terminal hoje", icon: "chart.bar.xaxis")
            if snapshot.salesByTerminal.isEmpty {
                EmptyState(title: "Nenhuma venda hoje", message: "As vendas concluídas aparecerão aqui.", icon: "chart.bar.xaxis")
            } else {
                Chart(snapshot.salesByTerminal) { terminal in
                    BarMark(x: .value("Faturamento", Double(terminal.revenueCents) / 100),
                            y: .value("Terminal", terminal.terminalName))
                        .foregroundStyle(LinearGradient(colors: [PDVTheme.blue, PDVTheme.mint], startPoint: .leading, endPoint: .trailing))
                        .cornerRadius(5)
                }
                .frame(height: max(150, CGFloat(snapshot.salesByTerminal.count) * 42))
                .chartXAxis { AxisMarks { AxisGridLine(); AxisValueLabel() } }
                .chartYAxis { AxisMarks { AxisValueLabel() } }
                .chartLegend(.hidden)
                ForEach(snapshot.salesByTerminal) { terminal in
                    HStack {
                        VStack(alignment: .leading, spacing: 3) {
                            Text(terminal.terminalName).font(.subheadline.weight(.semibold))
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
        VStack(alignment: .leading, spacing: 15) {
            sectionTitle("Terminais", subtitle: "Conexão e último contato", icon: "point.3.connected.trianglepath.dotted")
            ForEach(snapshot.terminals) { terminal in
                HStack(spacing: 12) {
                    ZStack {
                        Circle().fill(statusColor(terminal).opacity(0.13)).frame(width: 42, height: 42)
                        Image(systemName: terminal.revoked ? "lock.fill" : "desktopcomputer")
                            .font(.subheadline.bold()).foregroundStyle(statusColor(terminal))
                    }
                    VStack(alignment: .leading, spacing: 4) {
                        Text(terminal.name).font(.subheadline.weight(.semibold))
                        Text("\(terminal.shopName) • \(shortDate(terminal.lastSeenAt))")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Text(statusText(terminal))
                        .font(.caption2.weight(.bold)).foregroundStyle(statusColor(terminal))
                        .padding(.horizontal, 9).padding(.vertical, 6)
                        .background(statusColor(terminal).opacity(0.12), in: Capsule())
                }
                if terminal.id != snapshot.terminals.last?.id { Divider().padding(.leading, 54) }
            }
        }
        .monitorCard()
    }

    private func sectionTitle(_ title: String, subtitle: String, icon: String) -> some View {
        HStack(spacing: 11) {
            Image(systemName: icon).font(.headline).foregroundStyle(PDVTheme.blue)
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.headline)
                Text(subtitle).font(.caption).foregroundStyle(.secondary)
            }
        }
    }

    private func statusColor(_ terminal: TerminalStatus) -> Color {
        terminal.revoked ? .red : (terminal.isOnline ? .green : .orange)
    }

    private func statusText(_ terminal: TerminalStatus) -> String {
        terminal.revoked ? "Revogado" : (terminal.isOnline ? "Online" : "Offline")
    }
}

private struct HeroStat: View {
    let title: String
    let value: String

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(value).font(.headline.weight(.bold)).foregroundStyle(.white)
            Text(title).font(.caption).foregroundStyle(.white.opacity(0.68))
        }
        .frame(minWidth: 82, alignment: .leading)
    }
}

private struct MetricCard: View {
    let title: String
    let value: String
    let icon: String
    let color: Color

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Image(systemName: icon).font(.headline.bold()).foregroundStyle(color)
            Text(value).font(.title3.bold()).lineLimit(1).minimumScaleFactor(0.7)
            Text(title).font(.caption).foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .monitorCard()
    }
}

private struct EmptyState: View {
    let title: String
    let message: String
    let icon: String

    var body: some View {
        VStack(spacing: 8) {
            Image(systemName: icon).font(.title2).foregroundStyle(.secondary)
            Text(title).font(.subheadline.weight(.semibold))
            Text(message).font(.caption).foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 18)
    }
}

private extension String {
    var asBrazilDate: String {
        guard let date = asDate else { return self }
        return date.formatted(.dateTime.day().month(.wide).locale(Locale(identifier: "pt_BR")))
    }
}
