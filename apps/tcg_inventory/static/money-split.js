// Splits an amount of kr evenly into `n` shares rounded to øre, the last
// share taking the rounding so the shares always add up to exactly the
// amount: 100 over 3 is 33.33 / 33.33 / 33.34 (#312). Shared by the New Order
// cart's "Distribute evenly across empty prices" and the Facebook wins
// panel's "Link selected" for a lot linked to several cards (#309), both in
// static/orders-cart.js. Its own file, with no DOM use, so the root pytest
// can run it under node (tests/test_money_split.py).
function splitEvenly(amount, n) {
  if (!(n > 0) || !isFinite(amount)) return [];
  var cents = Math.round(amount * 100);
  var each = Math.round(cents / n);
  var shares = [];
  for (var i = 0; i < n - 1; i++) shares.push(each / 100);
  shares.push((cents - each * (n - 1)) / 100);
  return shares;
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { splitEvenly: splitEvenly };
}
