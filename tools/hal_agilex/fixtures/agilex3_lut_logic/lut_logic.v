// Purely combinational design that forces Quartus to emit normal-mode ALM LUTs.
module lut_logic (
    input  wire a,
    input  wire b,
    input  wire c,
    input  wire d,
    input  wire e,
    input  wire f,
    output wire y0,
    output wire y1,
    output wire y2
);

    assign y0 = (a & b) | ((~c) & d) | (e ^ f);
    assign y1 = (a ^ b ^ c) & (d | (~e));
    assign y2 = (a & (~b) & c) | (d & e & (~f)) | ((~a) & f);

endmodule
