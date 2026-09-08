// Design that deliberately pulls in blocks outside the LUT/FF/carry coverage:
// an inferred memory block and a hard multiplier, next to a small LUT+FF part.
module mixed_blocks (
    input  wire        clk,
    input  wire        we,
    input  wire [9:0]  waddr,
    input  wire [9:0]  raddr,
    input  wire [7:0]  wdata,
    input  wire [7:0]  opa,
    input  wire [7:0]  opb,
    output reg  [7:0]  rdata,
    output reg  [15:0] product,
    output reg         flag
);

    reg [7:0] mem [0:1023];

    always @(posedge clk) begin
        if (we) begin
            mem[waddr] <= wdata;
        end
        rdata   <= mem[raddr];
        product <= opa * opb;
        flag    <= (opa[0] & opb[1]) | (~opa[2]);
    end

endmodule
