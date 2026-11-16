import SwiftUI

/// Detalhe da venda: itens, pagamentos, descontos, devoluções e cancelamento.
struct SaleDetailView: View {
    @EnvironmentObject private var app: AppState
    @StateObject private var state = LoadState<SaleDetail>()
    let saleID: String
    let tenantID: String

    var body: some View {
        LoadedScrollView(state: state, key: saleID,
                         load: { try await app.api.sale(id: saleID, tenantID: tenantID) }) { sale in
            SaleDetailContent(sale: sale)
        }
        .navigationTitle("Venda")
        .navigationBarTitleDisplayMode(.inline)
    }
}

struct SaleDetailContent: View {
    let sale: SaleDetail

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    Text(Format.money(sale.totalCents)).font(.largeTitle.bold()).monospacedDigit()
                    Spacer()
                    StatusBadge.sale(sale.status)
                }
                ValueRow(title: "Data", value: Format.dateTime(sale.createdAt))
                ValueRow(title: "Loja", value: sale.shop.name)
                optionalRow("Caixa", [sale.terminalName, sale.stationName].filter { !$0.isEmpty }.joined(separator: " / "))
                optionalRow("Operador", sale.operatorName)
                optionalRow("Vendedor", sale.sellerName)
                optionalRow("Cliente", sale.customerName)
            }
            .card()

            VStack(alignment: .leading, spacing: 10) {
                Text("Itens").font(.headline)
                ForEach(sale.items) { item in
                    VStack(alignment: .leading, spacing: 3) {
                        HStack(alignment: .firstTextBaseline) {
                            Text(item.name).font(.subheadline.weight(.medium))
                            Spacer()
                            Text(Format.money(item.lineTotalCents)).font(.subheadline.weight(.semibold)).monospacedDigit()
                        }
                        Text("\(Format.quantity(milli: item.quantityMilli, unit: item.unit)) × \(Format.money(item.unitPriceCents))")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        if item.promoDiscountCents + item.discountCents > 0 {
                            Text("Desconto: \(Format.money(item.promoDiscountCents + item.discountCents))")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                    }
                    .accessibilityElement(children: .combine)
                    if item.id != sale.items.last?.id { Divider() }
                }
            }
            .card()

            VStack(alignment: .leading, spacing: 8) {
                Text("Pagamentos").font(.headline)
                ForEach(sale.payments) { payment in
                    ValueRow(title: payment.label.isEmpty ? Labels.payment(payment.method) : payment.label,
                             value: Format.money(payment.amountCents))
                }
                if sale.changeCents > 0 {
                    ValueRow(title: "Troco", value: Format.money(sale.changeCents))
                }
            }
            .card()

            VStack(alignment: .leading, spacing: 8) {
                Text("Descontos").font(.headline)
                ValueRow(title: "Valor bruto", value: Format.money(sale.grossCents))
                ValueRow(title: "Desconto de promoção", value: Format.money(sale.promoDiscountCents))
                ValueRow(title: "Desconto manual", value: Format.money(sale.discountCents))
                ValueRow(title: "Total", value: Format.money(sale.totalCents), emphasized: true)
            }
            .card()

            if !sale.returns.isEmpty {
                VStack(alignment: .leading, spacing: 10) {
                    Text("Devoluções").font(.headline)
                    ForEach(sale.returns) { item in
                        VStack(alignment: .leading, spacing: 3) {
                            ValueRow(title: Format.dateTime(item.createdAt), value: Format.money(item.totalCents))
                            Text(item.refundLabel.isEmpty ? Labels.refund(item.refundMethod) : item.refundLabel)
                                .font(.caption).foregroundStyle(.secondary)
                            if !item.reason.isEmpty {
                                Text("Motivo: \(item.reason)").font(.caption).foregroundStyle(.secondary)
                            }
                        }
                    }
                }
                .card()
            }

            if sale.cancelledAt != nil || !sale.cancellationReason.isEmpty {
                VStack(alignment: .leading, spacing: 8) {
                    Text("Cancelamento").font(.headline)
                    ValueRow(title: "Quando", value: Format.dateTime(sale.cancelledAt))
                    if !sale.cancellationReason.isEmpty {
                        Text("Motivo: \(sale.cancellationReason)").font(.subheadline)
                    }
                }
                .card()
            }
        }
    }

    @ViewBuilder
    private func optionalRow(_ title: String, _ value: String) -> some View {
        if !value.isEmpty { ValueRow(title: title, value: value) }
    }
}

#Preview {
    NavigationStack { SaleDetailView(saleID: "1", tenantID: "t") }.environmentObject(AppState.preview())
}
