// ADR-0049: standalone conformance module, deliberately not imported by a page.
const NORMAL_MIN = 2 ** -1022;
const syntax = /^[+-]?\d+(?:\.\d+)?(?:[eE]([+-]?\d+))?$/;

function rational(source) {
  const [mantissa, exponent = '0'] = source.toLowerCase().split('e');
  const negative = mantissa.startsWith('-');
  const unsigned = mantissa.replace(/^[+-]/, '');
  const [whole, fraction = ''] = unsigned.split('.');
  const coefficient = BigInt(whole + fraction) * (negative ? -1n : 1n);
  const power = Number(exponent) - fraction.length;
  return power >= 0 ? [coefficient * 10n ** BigInt(power), 1n]
                    : [coefficient, 10n ** BigInt(-power)];
}

export function preparePrice(source) {
  if (typeof source !== 'string' || source.length > 128) return { reason: 'invalid_source_price' };
  const match = syntax.exec(source);
  if (!match || Math.abs(Number(match[1] || 0)) > 400) return { reason: 'invalid_source_price' };
  const [n, d] = rational(source);
  if (n <= 0n) return { reason: 'nonpositive_price' };
  const number = Number(source);
  if (!Number.isFinite(number)) return { reason: 'binary64_overflow' };
  if (number === 0) return { reason: 'binary64_underflow' };
  if (number < NORMAL_MIN) return { reason: 'subnormal_price_unsupported' };
  const [backN, backD] = rational(String(number));
  if (backN * d !== n * backD) return { reason: 'binary64_roundtrip_loss' };
  return { source, number, n, d, reason: null };
}

function display(current, reference) {
  const digits = 2;
  const numerator = current.n * reference.d - reference.n * current.d;
  const denominator = current.d * reference.n;
  const scaled = (numerator < 0n ? -numerator : numerator) * 100n * 10n ** BigInt(digits);
  let units = scaled / denominator;
  const remainder = scaled % denominator;
  if (2n * remainder > denominator || (2n * remainder === denominator && units % 2n === 1n)) units++;
  const sign = units === 0n ? '' : numerator > 0n ? '+' : '-';
  const scale = 10n ** BigInt(digits);
  return sign + (units / scale).toString() + '.' + (units % scale).toString().padStart(digits, '0') + '%';
}

export function parityCase(currentSource, referenceSource) {
  const current = preparePrice(currentSource), reference = preparePrice(referenceSource);
  if (current.reason || reference.reason) return { status: 'unavailable', reason: current.reason || reference.reason };
  const fraction = (current.number - reference.number) / reference.number;
  if (!Number.isFinite(fraction)) return { status: 'unavailable', reason: 'binary64_arithmetic_unavailable' };
  return { status: 'available', reason: null, fraction, display: display(current, reference),
    current_source: current.source, reference_source: reference.source };
}
