import SwiftUI

/// Detalhe do turno: resumo, formas de pagamento, sangrias e suprimentos.
struct CashDetailView: View {
    @EnvironmentObject private var app: AppState
    @StateObject private var state = LoadState<CashSessionDetail>()
    let sessionID: String
    let tenantID: String

    var body: some View {
        LoadedScrollView(state: state, key: sessionID,
                         load: { try await app.api.cashSession(id: sessionID, tenantID: tenantID) }) { detail in
            CashDetailContent(detail: detail)
        }
        .navigationTitle("Caixa")
        .navigationBarTitleDisplayMode(.inline)
    }
}

struct CashDetailContent: View {
    let detail: CashSessionDetail

    var body: some View {
        let session = detail.session
        VStack(alignment: .leading, spacing: 14) {
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    Text(CashSessionRow(session: session).titleText).font(.title3.bold())
                    Spacer()
                    StatusBadge(text: Labels.cashStatus(session.status), tone: session.status == "open" ? .info : .neutral)
                }
                ValueRow(title: "Loja", value: session.shop.name)
                if !session.operatorName.isEmpty { ValueRow(title: "Operador", value: session.operatorName) }
                ValueRow(title: "Aberto em", value: Format.dateTime(session.openedAt))
                if session.closedAt != nil { ValueRow(title: "Fechado em", value: Format.dateTime(session.closedAt)) }
                if !detail.closedByName.isEmpty { ValueRow(title: "Fechado por", value: detail.closedByName) }
                if !detail.supervisorName.isEmpty { ValueRow(title: "Supervisor", value: detail.supervisorName) }
                if session.incomplete {
                    Text("Abertura não recebida").font(.subheadline).foregroundStyle(.orange)
                }
            }
            .card()

            VStack(alignment: .leading, spacing: 8) {
                Text("Resumo").font(.headline)
                ValueRow(title: "Fundo de troco", value: Format.money(session.openingCents))
                ValueRow(title: "Vendas", value: "\(session.salesCount) · \(Format.money(session.salesTotalCents))")
                ValueRow(title: "Dinheiro (líquido)", value: Format.money(session.cashNetCents))
                ValueRow(title: "Suprimentos", value: Format.money(session.suppliedCents))
                ValueRow(title: "Sangrias", value: Format.money(session.withdrawnCents))
                ValueRow(title: "Fiado recebido", value: Format.money(detail.creditReceivedCents))
                if session.status == "closed" {
                    Divider()
                    ValueRow(title: "Esperado", value: session.expectedCents.map(Format.money) ?? "—")
                    ValueRow(title: "Contado", value: session.countedCents.map(Format.money) ?? "—")
                    ValueRow(title: "Diferença", value: Format.cashDifference(session.differenceCents), emphasized: true)
                }
            }
            .card()

            VStack(alignment: .leading, spacing: 8) {
                Text("Formas de pagamento").font(.headline)
                if detail.byMethod.isEmpty {
                    Text("Nenhum pagamento neste caixa.").font(.subheadline).foregroundStyle(.secondary)
                }
                ForEach(detail.byMethod) { item in
                    ValueRow(title: item.label.isEmpty ? Labels.payment(item.method) : item.label,
                             value: Format.money(item.amountCents))
                }
            }
            .card()

            VStack(alignment: .leading, spacing: 10) {
                Text("Sangrias e suprimentos").font(.headline)
                if detail.movements.isEmpty {
                    Text("Nenhuma movimentação.").font(.subheadline).foregroundStyle(.secondary)
                }
                ForEach(detail.movements) { movement in
                    VStack(alignment: .leading, spacing: 3) {
                        ValueRow(title: movement.label.isEmpty ? Labels.movement(movement.kind) : movement.label,
                                 value: Format.money(movement.amountCents))
                        Text([Format.dateTime(movement.createdAt), movement.operatorName, movement.reason]
                            .filter { !$0.isEmpty }.joined(separator: " · "))
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
            }
            .card()
        }
    }
}

extension CashSessionRow {
    var titleText: String {
        [session.terminalName, session.stationName].filter { !$0.isEmpty }.joined(separator: " / ").ifEmpty("Caixa")
    }
}

#Preview {
    NavigationStack { CashDetailView(sessionID: "c", tenantID: "t") }.environmentObject(AppState.preview())
}
