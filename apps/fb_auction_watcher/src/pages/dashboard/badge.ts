import { saleTypeLabel, type SaleType } from "../../domain/listing";

// The sale-type badge next to a sale's name (#322): Auction / Claim / Fixed price / Unknown, in
// words and in the type's colour (dashboard.css `.type-badge.<type>`), so colour is never the only cue.

const TYPE_HINT: Record<SaleType, string> = {
  auction: "Auction: the highest bid when it ends wins",
  claim: "Claim sale: the first to comment gets to buy",
  fixed: "Fixed price: sold at the seller's price, no end time",
  wanted: "Wanted: someone looking to buy",
  trade: "Trade: someone looking to swap",
  other: "Unknown: the post doesn't say auction, claim or fixed price. Check the seller's text.",
};

export function typeBadge(type: SaleType): HTMLSpanElement {
  const badge = document.createElement("span");
  badge.className = `type-badge ${type}`;
  badge.textContent = saleTypeLabel(type);
  badge.title = TYPE_HINT[type];
  return badge;
}
