ObjC.import('Foundation');
ObjC.import('CoreImage');
ObjC.import('AppKit');

function run(argv) {
  if (argv.length < 2) { throw new Error('usage: qrgen.js <text> <out.png> [px]'); }
  const text = argv[0], out = argv[1], side = argv.length > 2 ? parseFloat(argv[2]) : 600;

  const data = $.NSString.alloc.initWithUTF8String(text)
                 .dataUsingEncoding($.NSUTF8StringEncoding);
  const filter = $.CIFilter.filterWithName('CIQRCodeGenerator');
  filter.setDefaults;
  filter.setValueForKey(data, 'inputMessage');
  filter.setValueForKey($.NSString.alloc.initWithUTF8String('M'), 'inputCorrectionLevel');

  const small = filter.outputImage;
  const scale = side / small.extent.size.width;
  const big = small.imageByApplyingTransform($.CGAffineTransformMakeScale(scale, scale));

  const rep = $.NSCIImageRep.imageRepWithCIImage(big);
  const img = $.NSImage.alloc.initWithSize(rep.size);
  img.addRepresentation(rep);
  const bmp = $.NSBitmapImageRep.imageRepWithData(img.TIFFRepresentation);
  const png = bmp.representationUsingTypeProperties($.NSPNGFileType, $());
  png.writeToFileAtomically($.NSString.alloc.initWithUTF8String(out), true);
  return out;
}
