// Source of the checked-in iCE40 fixture bitstream (see README.md).
//
// Deliberately tiny and deliberately sequential: the counter forces flip-flops and carry logic
// into the bitstream, so a conversion that loses the sequential elements cannot pass the smoke
// test. Nothing here is iCE40-specific -- yosys' synth_ice40 maps it onto SB_LUT4/SB_CARRY/SB_DFF.
module blinky (
    input  wire clk,
    input  wire rst,
    output wire led
);
    reg [7:0] counter;

    always @(posedge clk) begin
        if (rst)
            counter <= 8'd0;
        else
            counter <= counter + 8'd1;
    end

    assign led = counter[7];
endmodule
