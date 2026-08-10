ObjC.import('Foundation');
ObjC.import('CoreImage');
function run(argv) {
  const url = $.NSURL.fileURLWithPath($.NSString.alloc.initWithUTF8String(argv[0]));
  const img = $.CIImage.imageWithContentsOfURL(url);
  const det = $.CIDetector.detectorOfTypeContextOptions('CIDetectorTypeQRCode', $(), $());
  const found = det.featuresInImage(img);
  if (found.count === 0) return 'NO QR FOUND';
  const out = [];
  for (let i = 0; i < found.count; i++) {
    out.push(ObjC.unwrap(found.objectAtIndex(i).messageString));
  }
  return out.join('\n');
}
